"""INT-022（docs/testing/04 §1C）：后台化 OAuth 登录全流程。

真实网关进程（ASGI）+ Mock 上游（真实端口）：
login/start → （测试侧模拟用户授权：切 Mock oauth_state）→ login/poll → 凭证入池。
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestOAuthLoginFlow:
    async def test_start_returns_clickable_url(self, gateway_client):
        client, mock = gateway_client
        res = await client.post("/admin/api/login/start", json={"label": "acct-1"},
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        data = res.json()
        assert data["flow_id"].startswith("mock-flow-")
        assert data["authorize_url"].startswith("https://")
        assert data["expires_in"] == 300
        # init 请求真的打到了上游
        assert mock.state.calls[-1][1] == "/api/v1/oauth/cli/init"

    async def test_full_flow_ready_pools_account(self, gateway_client):
        client, mock = gateway_client
        start = (await client.post("/admin/api/login/start", json={"label": "acct-1"},
                                   headers={"Authorization": "Bearer zcode"})).json()
        fid = start["flow_id"]

        # 模拟用户在浏览器点同意
        mock.state.oauth_state = "ready"
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        assert poll["status"] == "ready"
        acc = poll["account"]
        assert acc["name"] == "acct-1"
        assert acc["mode"] == "jwt"
        assert acc["provider"] == "zai"
        # JWT 已脱敏
        assert "mock-gateway-jwt-header" not in acc["token_masked"]

        # 入池可查、可服务
        accounts = (await client.get("/admin/api/accounts",
                                     headers={"Authorization": "Bearer zcode"})).json()
        assert any(a["id"] == acc["id"] for a in accounts["accounts"])
        assert accounts["stats"]["active"] == 1

        # 会话已清理：再 poll 404
        res = await client.get(f"/admin/api/login/poll/{fid}",
                               headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 404

    async def test_denied_flow_reports_failure_reason(self, gateway_client):
        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start",
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        mock.state.oauth_state = "failed"
        mock.state.oauth_fail_message = "user denied"
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        assert poll["status"] == "failed"
        assert poll["message"] == "user denied"
        # 失败后无账号入池
        accounts = (await client.get("/admin/api/accounts",
                                     headers={"Authorization": "Bearer zcode"})).json()
        assert accounts["stats"]["total"] == 0

    async def test_poll_before_authorize_is_pending(self, gateway_client):
        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start",
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        mock.state.oauth_state = "pending"
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        assert poll["status"] == "pending"

    async def test_unknown_flow_404(self, gateway_client):
        client, _ = gateway_client
        res = await client.get("/admin/api/login/poll/nonexistent",
                               headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 404

    async def test_admin_key_required(self, gateway_client):
        client, _ = gateway_client
        res = await client.post("/admin/api/login/start")
        assert res.status_code == 401

    async def test_exchange_api_key_chain_on_ready(self, gateway_client):
        """ready 后应完整走兑换链（z/login → getCustomerInfo → create → copy），
        兑换出的 apiKey 回填到账号（api_key 模式作为 JWT 的回退凭证）。"""
        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start", json={"label": "acct-2"},
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        mock.state.oauth_state = "ready"
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        assert poll["status"] == "ready"
        paths = [c[1] for c in mock.state.calls]
        assert "/api/auth/z/login" in paths
        assert "/api/biz/customer/getCustomerInfo" in paths
        assert any(p.endswith("/api_keys") for p in paths)
        # copy secret → apiKey.secretKey 组合入账（agent 回退通道用）
        assert any(p.endswith("/api_keys/copy/mock-api-key-id") for p in paths)
        # ready 后立即触发一次额度刷新（billing 三端点）
        assert any(p.endswith("/billing/current") for p in paths)
