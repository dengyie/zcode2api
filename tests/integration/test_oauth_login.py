"""INT-022（docs/testing/04 §1C）：后台化 OAuth 登录全流程。

真实网关进程（ASGI）+ Mock 上游（真实端口）：
login/start → （测试侧模拟用户授权：切 Mock oauth_state）→ login/poll → 凭证入池。
"""

from __future__ import annotations

import asyncio

import pytest


def _oauth_header_names(headers: dict) -> set[str]:
    return {str(k).lower() for k in headers}


async def _drain_login_followup() -> None:
    from app.routes import admin_api

    pending = list(admin_api._login_followup_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.integration
class TestOAuthLoginFlow:
    @pytest.fixture(autouse=True)
    async def _reset_oauth_mock(self, gateway_client):
        _, mock = gateway_client
        mock.state.oauth_state = "pending"
        mock.state.oauth_poll_status = 200
        mock.state.oauth_login_fail = False
        mock.state.oauth_exchange_delay = 0
        mock.state.oauth_fail_message = "user denied"
        yield
        await _drain_login_followup()

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

    async def test_init_and_poll_headers_match_official_cli(self, gateway_client):
        """官方 CLI 契约：init 仅 Authorization + Content-Type；poll 仅 Authorization。

        禁止再把 User-Agent=ZCode/…、X-Platform 等身份头绑进 CLI 会话。
        """
        client, mock = gateway_client
        start = (await client.post("/admin/api/login/start",
                                   headers={"Authorization": "Bearer zcode"})).json()
        init_headers = next(h for m, p, h, _ in mock.state.calls if p.endswith("/oauth/cli/init"))
        init_names = _oauth_header_names(init_headers)
        assert "authorization" in init_names
        assert "content-type" in init_names
        assert "x-platform" not in init_names
        assert "x-zcode-app-version" not in init_names
        assert "x-device-mid" not in init_names
        assert not str(init_headers.get("user-agent") or "").startswith("ZCode/")

        await client.get(f"/admin/api/login/poll/{start['flow_id']}",
                         headers={"Authorization": "Bearer zcode"})
        poll_headers = next(h for m, p, h, _ in mock.state.calls if "/oauth/cli/poll/" in p)
        poll_names = _oauth_header_names(poll_headers)
        assert "authorization" in poll_names
        assert "x-platform" not in poll_names
        assert "x-zcode-app-version" not in poll_names
        assert not str(poll_headers.get("user-agent") or "").startswith("ZCode/")

    async def test_poll_http_4xx_is_failed_not_pending(self, gateway_client):
        """官方 poll 明确 4xx 时必须 failed（并摘会话），不能静默成 pending。"""
        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start",
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        mock.state.oauth_poll_status = 401
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        assert poll["status"] == "failed"
        assert "401" in str(poll.get("message") or "")
        poll2 = (await client.get(f"/admin/api/login/poll/{fid}",
                                  headers={"Authorization": "Bearer zcode"})).json()
        assert poll2["status"] == "expired"
        accounts = (await client.get("/admin/api/accounts",
                                     headers={"Authorization": "Bearer zcode"})).json()
        assert accounts["stats"]["total"] == 0

    async def test_poll_http_5xx_stays_pending(self, gateway_client):
        """官方 poll 5xx / 抖动保持 pending，会话不摘除。"""
        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start",
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        mock.state.oauth_poll_status = 502
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        assert poll["status"] == "pending"
        mock.state.oauth_poll_status = 200
        mock.state.oauth_state = "pending"
        poll2 = (await client.get(f"/admin/api/login/poll/{fid}",
                                  headers={"Authorization": "Bearer zcode"})).json()
        assert poll2["status"] == "pending"

    async def test_ready_returns_before_api_key_exchange(self, gateway_client):
        """JWT 入池必须在兑换链完成前返回 ready，避免前端下一轮 poll 误判 expired。"""
        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start", json={"label": "acct-fast"},
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        mock.state.oauth_state = "ready"
        mock.state.oauth_exchange_delay = 0.6
        t0 = asyncio.get_event_loop().time()
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        elapsed = asyncio.get_event_loop().time() - t0
        assert poll["status"] == "ready"
        assert elapsed < 0.4
        accounts = (await client.get("/admin/api/accounts",
                                     headers={"Authorization": "Bearer zcode"})).json()
        assert accounts["stats"]["total"] == 1
        poll2 = (await client.get(f"/admin/api/login/poll/{fid}",
                                  headers={"Authorization": "Bearer zcode"})).json()
        assert poll2["status"] == "expired"

    async def test_exchange_failure_still_pools_jwt(self, gateway_client):
        """兑换链 5xx 不得丢掉已拿到的 JWT；账号仍入池。"""
        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start", json={"label": "acct-jwt-only"},
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        mock.state.oauth_state = "ready"
        mock.state.oauth_login_fail = True
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        assert poll["status"] == "ready"
        assert poll["account"]["mode"] == "jwt"
        accounts = (await client.get("/admin/api/accounts",
                                     headers={"Authorization": "Bearer zcode"})).json()
        assert accounts["stats"]["total"] == 1

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

        # 会话已摘除：再 poll 返回 expired（契约见 admin_api.login_poll docstring）
        poll2 = (await client.get(f"/admin/api/login/poll/{fid}",
                                  headers={"Authorization": "Bearer zcode"})).json()
        assert poll2["status"] == "expired"

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

    async def test_unknown_flow_expired(self, gateway_client):
        """未知 flow_id 与超时会话同响应 {"status": "expired"}（幂等，无 404）。"""
        client, _ = gateway_client
        res = await client.get("/admin/api/login/poll/nonexistent",
                               headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        assert res.json()["status"] == "expired"

    async def test_flow_ttl_expiry(self, gateway_client, monkeypatch):
        """超过 LOGIN_FLOW_TTL 的会话被 GC，poll 返回 expired。"""
        from app.routes import admin_api

        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start",
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        assert fid in admin_api._login_flows

        # 快进时钟：把会话创建时间拨回 TTL+1 秒之前
        entry = admin_api._login_flows[fid]
        entry["created"] -= admin_api.LOGIN_FLOW_TTL + 1.0
        poll = (await client.get(f"/admin/api/login/poll/{fid}",
                                 headers={"Authorization": "Bearer zcode"})).json()
        assert poll["status"] == "expired"
        assert fid not in admin_api._login_flows

    async def test_ready_poll_reentry_protected(self, gateway_client):
        """P2-1 回归：ready 后会话已摘除再兑换，重复/并发 poll 不会触发第二份
        兑换链（z/login → getCustomerInfo → api_keys/copy），也不会重复入池。"""
        client, mock = gateway_client
        fid = (await client.post("/admin/api/login/start", json={"label": "acct-3"},
                                 headers={"Authorization": "Bearer zcode"})).json()["flow_id"]
        # mock 上游 session 级共享，calls 跨用例累积 —— 记录基线后断言增量
        copy_calls_before = sum(1 for c in mock.state.calls
                                if c[1].endswith("/api_keys/copy/mock-api-key-id"))
        mock.state.oauth_state = "ready"
        first = (await client.get(f"/admin/api/login/poll/{fid}",
                                  headers={"Authorization": "Bearer zcode"})).json()
        assert first["status"] == "ready"

        # 无论如何并发再 poll 一轮 —— 已摘除的会话只能拿到 expired，绝不重入兑换
        results = await asyncio.gather(*[
            client.get(f"/admin/api/login/poll/{fid}",
                       headers={"Authorization": "Bearer zcode"}) for _ in range(4)
        ])
        assert all(r.json()["status"] == "expired" for r in results)

        await _drain_login_followup()
        # 兑换链只跑了一遍：copy 端点恰好 +1 次；账号只入池了 1 个
        copy_calls_after = sum(1 for c in mock.state.calls
                               if c[1].endswith("/api_keys/copy/mock-api-key-id"))
        assert copy_calls_after - copy_calls_before == 1
        accounts = (await client.get("/admin/api/accounts",
                                     headers={"Authorization": "Bearer zcode"})).json()
        assert accounts["stats"]["total"] == 1

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
        await _drain_login_followup()
        paths = [c[1] for c in mock.state.calls]
        assert "/api/auth/z/login" in paths
        assert "/api/biz/customer/getCustomerInfo" in paths
        assert any(p.endswith("/api_keys") for p in paths)
        # copy secret → apiKey.secretKey 组合入账（agent 回退通道用）
        assert any(p.endswith("/api_keys/copy/mock-api-key-id") for p in paths)
        # ready 后立即触发一次额度刷新（billing 三端点）
        assert any(p.endswith("/billing/current") for p in paths)
