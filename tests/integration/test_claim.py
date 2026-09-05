"""套餐领取（claim）集成测试 —— preview / claim / 失败码映射 / 验证码头。

链路：admin API → app.claim → Mock 上游 billing/preview|claim（验证码头校验）。
captcha 用桩（与 conftest._StubCaptcha 同款），聚焦请求形态与业务码语义。
"""

from __future__ import annotations

import pytest

from tests.conftest import seed_account

GOOD_JWT = "h1.eyJzdWIiOiJhIn0.sig"


@pytest.fixture
def claim_env(gateway_client, fresh_app, monkeypatch):
    """网关客户端 + JWT 账号 + claim 模块的验证码桩。"""
    client, mock = gateway_client
    from app import captcha as captcha_module
    from app import claim as claim_module

    class _StubCaptcha:
        def __init__(self):
            self.solve_count = 0
            self.invalidated = 0

        async def get_verify_param(self, port=None):
            self.solve_count += 1
            return "stub-verify-param", "sgp"

        async def fetch_config(self):
            return {"enabled": True, "prefix": "mockpre", "region": "sgp", "sceneId": "mock-scene"}

        def invalidate(self):
            self.invalidated += 1

    stub = _StubCaptcha()
    monkeypatch.setattr(claim_module, "captcha_manager", stub)
    monkeypatch.setattr(captcha_module, "captcha_manager", stub)
    mock.state.claim_scenario = None  # session 级 mock，防止场景跨用例残留
    acc = seed_account(fresh_app, GOOD_JWT, name="claim-a")
    return client, mock, stub, acc


@pytest.mark.integration
class TestClaim:
    async def test_preview_parses_and_filters(self, claim_env):
        client, mock, _stub, _acc = claim_env
        res = await client.get("/admin/api/claim/preview",
                               headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        entry = res.json()["preview"][0]
        assert entry["error"] is None
        plan = entry["plans"][0]
        assert plan["plan_id"] == "mock-claim-plan"
        # 只保留 model_usage+token 授权项，噪音项被过滤
        assert [g["name"] for g in plan["grants"]] == ["GLM-5.3"]
        assert plan["grants"][0]["units"] == 3_000_000
        assert plan["grants"][0]["period"] == "daily"

    async def test_preview_business_failure_surfaced(self, claim_env):
        client, mock, _stub, _acc = claim_env
        mock.state.claim_scenario = "claim_claimed"
        res = await client.get("/admin/api/claim/preview",
                               headers={"Authorization": "Bearer zcode"})
        entry = res.json()["preview"][0]
        assert entry["plans"] == []
        assert "已经领取过" in entry["error"]

    async def test_claim_success_sends_captcha_and_plan(self, claim_env):
        client, mock, stub, acc = claim_env
        res = await client.post("/admin/api/claim",
                                json={"account_ids": [acc.id]},
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is True
        assert outcome["plan_name"] == "Mock Daily Plan"
        assert res.json()["summary"] == {"ok": 1, "fail": 0}

        # 上游收到验证码头 + region 头 + plan_id body；验证码求解恰好 1 次
        claim_calls = [c for c in mock.state.calls if c[1].endswith("/billing/claim")]
        assert len(claim_calls) == 1
        _method, _path, headers, body = claim_calls[0]
        assert headers.get("x-aliyun-captcha-verify-param") == "stub-verify-param"
        assert headers.get("x-aliyun-captcha-verify-region") == "sgp"
        assert headers.get("x-device-mid")  # billing 全家桶必需（否则上游 3001）
        # 客户端 claim 头形态：缺版本/平台头即使验证码有效也 3007（实测）
        assert headers.get("x-zcode-app-version") == "3.11.2"  # BILLING_APP_VERSION
        assert headers.get("x-platform") == "darwin-arm64"
        assert b"mock-claim-plan" in body
        assert stub.solve_count == 1
        # 领取成功后触发额度刷新
        assert any(c[1].endswith("/billing/balance") for c in mock.state.calls)

    async def test_claim_auto_picks_plan_when_omitted(self, claim_env):
        client, mock, _stub, acc = claim_env
        res = await client.post("/admin/api/claim",
                                json={"account_ids": [acc.id]},
                                headers={"Authorization": "Bearer zcode"})
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is True
        assert outcome["plan_id"] == "mock-claim-plan"

    async def test_claim_captcha_rejected_retries_once(self, claim_env):
        client, mock, stub, acc = claim_env
        mock.state.claim_scenario = "claim_captcha_fail"
        res = await client.post("/admin/api/claim",
                                json={"account_ids": [acc.id]},
                                headers={"Authorization": "Bearer zcode"})
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is False
        assert "验证码校验失败" in outcome["message"]
        # 3007 → 换码重试一次：求解 2 次 + invalidate 1 次
        assert stub.solve_count == 2
        assert stub.invalidated == 1
        assert res.json()["summary"] == {"ok": 0, "fail": 1}

    async def test_preview_reports_activation_events(self, claim_env):
        """preview 前上报 app_launch + app_daily_active（zcode-switch 同形）。"""
        import json as _json

        client, mock, _stub, _acc = claim_env
        mock.state.calls.clear()  # session 级 mock，只看本用例的上游调用
        res = await client.get("/admin/api/claim/preview",
                               headers={"Authorization": "Bearer zcode"})
        entry = res.json()["preview"][0]
        assert entry["activated"] is True
        assert entry["activation_error"] is None

        events = [(h, b) for m, p, h, b in mock.state.calls if p == "/api/v1/event/report"]
        assert len(events) == 2
        names = [_json.loads(b)["element_name"] for _h, b in events]
        assert names == ["app_launch", "app_daily_active"]
        _h, body = events[0]
        body = _json.loads(body)
        # user_id 来自 JWT payload（GOOD_JWT sub="a" 兜底）；无 Authorization 头
        assert body["user_id"] == "a"
        assert body["app_version"] == "3.11.2"
        assert body["device_mid"]
        assert body["screen_resolution"] == "2560x1440"
        assert body["event_region"] == "app" and body["event_type"] == "view"
        assert events[0][0].get("authorization") is None

    async def test_billing_headers_aligned_on_preview(self, claim_env):
        """billing 请求头对齐 zcode-switch 实证形态（版本/标题/渠道/追踪头）。"""
        client, mock, _stub, _acc = claim_env
        mock.state.calls.clear()
        await client.get("/admin/api/claim/preview",
                         headers={"Authorization": "Bearer zcode"})
        preview_calls = [c for c in mock.state.calls
                         if c[1].endswith("/billing/preview")]
        assert preview_calls
        h = preview_calls[-1][2]
        assert h.get("user-agent") == "ZCode/3.11.2"
        assert h.get("x-zcode-app-version") == "3.11.2"
        assert h.get("x-title") == "Z Code@electron"
        assert h.get("x-release-channel") == "stable"
        assert h.get("x-client-language") == "zh-CN"
        assert h.get("x-os-category") == "macos"
        assert h.get("x-request-id")

    async def test_activation_failure_does_not_block_preview(self, claim_env):
        """激活上报失败只标记 activated=False，preview 照常返回。"""
        client, mock, _stub, _acc = claim_env
        mock.state.calls.clear()
        mock.state.event_report_fail = "down"
        res = await client.get("/admin/api/claim/preview",
                               headers={"Authorization": "Bearer zcode"})
        entry = res.json()["preview"][0]
        assert entry["activated"] is False
        assert "HTTP 500" in entry["activation_error"]
        assert entry["error"] is None
        assert entry["plans"], "preview 未被激活上报失败阻断"

    async def test_claim_already_claimed_no_retry(self, claim_env):
        client, mock, stub, acc = claim_env
        mock.state.claim_scenario = "claim_claimed"
        # 显式 plan_id 跳过 preview，直接命中 claim 端点的 1003
        res = await client.post("/admin/api/claim",
                                json={"account_ids": [acc.id], "plan_id": "mock-claim-plan"},
                                headers={"Authorization": "Bearer zcode"})
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is False
        assert "已经领取过" in outcome["message"]
        assert stub.solve_count == 1  # 非 3007 不重试

    async def test_claim_skips_api_key_accounts(self, claim_env, fresh_app):
        client, _mock, _stub, _acc = claim_env
        seed_account(fresh_app, "sk-big-1234567890abcdef", name="key-b")
        res = await client.post("/admin/api/claim", json={},
                                headers={"Authorization": "Bearer zcode"})
        outcomes = res.json()["outcomes"]
        # 只有 JWT 账号进入领取；apiKey 账号被过滤
        assert [o["account_name"] for o in outcomes] == ["claim-a"]

    async def test_claim_requires_admin_key(self, claim_env):
        client, _mock, _stub, acc = claim_env
        res = await client.post("/admin/api/claim", json={"account_ids": [acc.id]})
        assert res.status_code == 401


@pytest.mark.integration
class TestManualClaim:
    """手动领取：verifyParam 由用户浏览器滑块产生，服务端只转发（不调用求解器）。"""

    async def test_captcha_config_endpoint(self, claim_env):
        client, _mock, _stub, _acc = claim_env
        res = await client.get("/admin/api/claim/captcha-config",
                               headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        cfg = res.json()
        assert cfg["enabled"] is True
        assert cfg["scene_id"] == "mock-scene"
        assert cfg["region"] == "sgp"
        assert cfg["prefix"] == "mockpre"

    async def test_manual_claim_success_forwards_browser_param(self, claim_env):
        client, mock, stub, acc = claim_env
        calls_before = len(mock.state.calls)
        res = await client.post(
            "/admin/api/claim/manual",
            json={"account_id": acc.id, "captcha_verify_param": "browser-slider-param",
                  "captcha_region": "cn", "plan_id": "mock-claim-plan"},
            headers={"Authorization": "Bearer zcode"},
        )
        assert res.status_code == 200
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is True
        assert res.json()["summary"] == {"ok": 1, "fail": 0}
        # 浏览器参数原样转发；服务端求解器零调用
        claim_calls = [c for c in mock.state.calls[calls_before:]
                       if c[1].endswith("/billing/claim")]
        assert len(claim_calls) == 1
        _method, _path, headers, body = claim_calls[0]
        assert headers.get("x-aliyun-captcha-verify-param") == "browser-slider-param"
        assert headers.get("x-aliyun-captcha-verify-region") == "cn"
        assert headers.get("x-device-mid")
        assert headers.get("x-zcode-app-version") == "3.11.2"  # 客户端 claim 头形态
        assert headers.get("x-platform") == "darwin-arm64"
        assert b"mock-claim-plan" in body
        assert stub.solve_count == 0
        # 成功后触发额度刷新
        assert any(c[1].endswith("/billing/balance") for c in mock.state.calls[calls_before:])

    async def test_manual_claim_auto_picks_plan(self, claim_env):
        client, mock, _stub, acc = claim_env
        res = await client.post(
            "/admin/api/claim/manual",
            json={"account_id": acc.id, "captcha_verify_param": "browser-slider-param"},
            headers={"Authorization": "Bearer zcode"},
        )
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is True
        assert outcome["plan_id"] == "mock-claim-plan"
        _m, _p, headers, _body = next(
            c for c in mock.state.calls if c[1].endswith("/billing/claim"))
        assert headers.get("x-aliyun-captcha-verify-region") == "sgp"  # 缺省用 config.region

    async def test_manual_claim_without_param_rejected(self, claim_env):
        client, mock, stub, acc = claim_env
        calls_before = len(mock.state.calls)
        res = await client.post(
            "/admin/api/claim/manual",
            json={"account_id": acc.id},
            headers={"Authorization": "Bearer zcode"},
        )
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is False
        assert "缺少验证码参数" in outcome["message"]
        assert stub.solve_count == 0
        assert not [c for c in mock.state.calls[calls_before:]
                    if c[1].endswith("/billing/claim")]

    async def test_manual_claim_business_failure_surfaced(self, claim_env):
        client, mock, _stub, acc = claim_env
        mock.state.claim_scenario = "claim_claimed"
        res = await client.post(
            "/admin/api/claim/manual",
            json={"account_id": acc.id, "captcha_verify_param": "p",
                  "plan_id": "mock-claim-plan"},
            headers={"Authorization": "Bearer zcode"},
        )
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is False
        assert "已经领取过" in outcome["message"]
        assert res.json()["summary"] == {"ok": 0, "fail": 1}

    async def test_manual_claim_unknown_account_404(self, claim_env):
        client, _mock, _stub, _acc = claim_env
        res = await client.post(
            "/admin/api/claim/manual",
            json={"account_id": "no-such", "captcha_verify_param": "p"},
            headers={"Authorization": "Bearer zcode"},
        )
        assert res.status_code == 404

    async def test_manual_claim_requires_admin_key(self, claim_env):
        client, _mock, _stub, acc = claim_env
        res = await client.post("/admin/api/claim/manual",
                                json={"account_id": acc.id, "captcha_verify_param": "p"})
        assert res.status_code == 401
