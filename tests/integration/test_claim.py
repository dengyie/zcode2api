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
        # 指纹档案（2026-09-07）：事件字段按账号指纹出值，与 billing 头同源
        from app.fingerprint import profile_for

        profile = profile_for(_acc)
        assert body["screen_resolution"] == profile.screen
        assert body["device_mid"] == profile.device_mid
        assert body["device_os_category"] == profile.os_category
        assert body["client_timezone"] == profile.timezone
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
        try:
            res = await client.get("/admin/api/claim/preview",
                                   headers={"Authorization": "Bearer zcode"})
            entry = res.json()["preview"][0]
            assert entry["activated"] is False
            assert "HTTP 500" in entry["activation_error"]
            assert entry["error"] is None
            assert entry["plans"], "preview 未被激活上报失败阻断"
        finally:
            mock.state.event_report_fail = None  # session 级 mock，防止污染后续用例

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

    async def test_claim_captcha_solve_failure_business_receipt(self, claim_env, monkeypatch):
        """验证码求解最终失败（CaptchaSolveError）→ 200 业务回执，而非 500（2026-09-07）。"""
        from app.captcha import CaptchaSolveError

        client, _mock, stub, acc = claim_env

        async def _exhausted(port=None):
            raise CaptchaSolveError("验证码求解失败: 多次重试无结果")

        monkeypatch.setattr(type(stub), "get_verify_param", _exhausted)
        res = await client.post("/admin/api/claim",
                                json={"account_ids": [acc.id], "plan_id": "mock-claim-plan"},
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is False
        assert "验证码求解失败" in outcome["message"]

    async def test_claim_upstream_network_error_business_receipt(self, claim_env, monkeypatch):
        """billing 上游网络故障 → ClaimError 回执（200），不再裸 500（2026-09-07）。"""
        import httpx

        client, mock, _stub, acc = claim_env

        class _DeadClient(httpx.AsyncClient):
            async def request(self, *args, **kwargs):
                raise httpx.ConnectError("connection refused")

        # 仅替换 claim 模块视角的 AsyncClient（测试自身传输不受影响）
        monkeypatch.setattr(httpx, "AsyncClient", _DeadClient)
        before = sum(c[1].endswith("/billing/claim") for c in mock.state.calls)
        res = await client.post("/admin/api/claim",
                                json={"account_ids": [acc.id], "plan_id": "mock-claim-plan"},
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is False
        assert "上游网络错误" in outcome["message"]
        after = sum(c[1].endswith("/billing/claim") for c in mock.state.calls)
        assert after == before  # 请求未到达上游

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


@pytest.mark.integration
class TestAutoClaimOnPoolEntry:
    """入池即激活+自动领取（2026-09-06）：新 JWT 账号入池后台自动吃满活动。"""

    async def test_auto_claim_all_plans_success(self, claim_env):
        from app.claim import auto_claim_all_plans

        client, mock, _stub, acc = claim_env
        mock.state.calls.clear()
        outcomes = await auto_claim_all_plans(acc)
        assert len(outcomes) == 1
        assert outcomes[0]["ok"] is True
        assert outcomes[0]["plan_id"] == "mock-claim-plan"
        # 激活 2 条 + preview 1 次 + claim 1 次
        paths = [p for _m, p, _h, _b in mock.state.calls]
        assert paths.count("/api/v1/event/report") == 2
        assert sum(p.endswith("/billing/preview") for p in paths) == 1
        assert sum(p.endswith("/billing/claim") for p in paths) == 1

    async def test_auto_claim_preview_failure_is_safe(self, claim_env):
        """preview 失败（如 1003）只记日志，不抛出。"""
        from app.claim import auto_claim_all_plans

        _client, mock, _stub, acc = claim_env
        mock.state.claim_scenario = "claim_claimed"
        assert await auto_claim_all_plans(acc) == []

    async def test_auto_claim_skips_non_jwt(self, claim_env, fresh_app):
        from app.claim import auto_claim_all_plans

        client, _mock, _stub, _acc = claim_env
        apikey_acc = seed_account(fresh_app, "sk-apikey-token", name="k")
        assert await auto_claim_all_plans(apikey_acc) == []

    async def test_add_account_schedules_auto_claim(self, claim_env, monkeypatch):
        import asyncio

        from app.routes import admin_api

        client, mock, _stub, _acc = claim_env
        called = []

        async def _spy(account):
            called.append(account.id)
            return []

        monkeypatch.setattr(admin_api, "auto_claim_all_plans", _spy)
        res = await client.post("/admin/api/accounts",
                                json={"provider": "zai", "tokens": [GOOD_JWT + "-new"]},
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        await asyncio.sleep(0.05)
        assert called, "新账号入池应调度自动领取"

        # 重复 token（去重后非新增）不再触发
        before = len(called)
        await client.post("/admin/api/accounts",
                          json={"provider": "zai", "tokens": [GOOD_JWT + "-new"]},
                          headers={"Authorization": "Bearer zcode"})
        await asyncio.sleep(0.05)
        assert len(called) == before

    async def test_import_schedules_auto_claim(self, claim_env, monkeypatch):
        import asyncio

        from app.routes import admin_api

        client, mock, _stub, _acc = claim_env
        called = []

        async def _spy(account):
            called.append(account.id)
            return []

        monkeypatch.setattr(admin_api, "auto_claim_all_plans", _spy)
        res = await client.post("/admin/api/import",
                                json={"providers": {"zai": [
                                    {"name": "imp", "secret": GOOD_JWT + "-imp"}]}},
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        await asyncio.sleep(0.05)
        assert called, "导入的新 JWT 账号应调度自动领取"

    async def test_add_account_assigns_distinct_fingerprints(self, claim_env):
        """入池即分配独立设备指纹：跨账号 device_mid 互异，且 public_view 可见。"""
        client, _mock, _stub, _acc = claim_env
        res = await client.post("/admin/api/accounts",
                                json={"provider": "zai",
                                      "tokens": [GOOD_JWT + "-fp1", GOOD_JWT + "-fp2"],
                                      "name": "fp"},
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        listing = await client.get("/admin/api/accounts",
                                   headers={"Authorization": "Bearer zcode"})
        fps = [a["fingerprint"] for a in listing.json()["accounts"]
               if a["name"] == "fp"]
        assert len(fps) == 2
        assert all(fp and fp.get("device_mid") for fp in fps)
        assert fps[0]["device_mid"] != fps[1]["device_mid"]

    async def test_rotate_fingerprint_endpoint(self, claim_env):
        """POST /accounts/{id}/fingerprint/rotate：换发新指纹并落库。"""
        client, _mock, _stub, acc = claim_env
        before = (await client.get("/admin/api/accounts",
                                   headers={"Authorization": "Bearer zcode"})).json()
        old_mid = next(a["fingerprint"]["device_mid"] for a in before["accounts"]
                       if a["id"] == acc.id)
        res = await client.post(f"/admin/api/accounts/{acc.id}/fingerprint/rotate",
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        new_fp = res.json()["fingerprint"]
        assert new_fp["device_mid"] != old_mid
        # 落库验证：重新拉取仍是新指纹
        after = (await client.get("/admin/api/accounts",
                                  headers={"Authorization": "Bearer zcode"})).json()
        cur = next(a["fingerprint"]["device_mid"] for a in after["accounts"]
                   if a["id"] == acc.id)
        assert cur == new_fp["device_mid"]

    async def test_rotate_fingerprint_unknown_account_404(self, claim_env):
        client, _mock, _stub, _acc = claim_env
        res = await client.post("/admin/api/accounts/nonexistent/fingerprint/rotate",
                                headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 404
