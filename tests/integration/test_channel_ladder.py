"""渠道优先梯（Magpie 模式 plan_std → zcode_plan → apikey）集成测试。

JWT + OAuth access_token 账号应有 plan_std 优先通道，标准端点 401 时下沉
下一渠道而非判死账号，1310 额度耗尽写入重置时间戳。

Mock bind 关键：plan_std 用 OAuth access_token 作 Bearer，zcode_plan 用 JWT
作 Bearer —— 两渠道分属不同 bind key。测试必须分设各自的序列。
"""

from __future__ import annotations

import pytest

from app.models import Status

_JWT_OAUTH = "cl1.eyJzdWIiOiJjbCJ9.sig"
_JWT_ONLY = "cl2.eyJzdWIiOiJjbDIifQ.sig"
# oauth_token 的第一个 16 字符（mock bind key）：plan_std 走这路
_OAUTH_BIND = "mock-oauth-toke"
_OAUTH_BIND_STALE = "stale-oauth-toke"
_MSG_BODY = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}]}


def _messages_calls(mock) -> list:
    return [c for c in mock.state.calls
            if c[0] == "POST" and c[1].endswith("/anthropic/v1/messages")]


@pytest.mark.integration
class TestChannelLadderPlanStd:
    async def test_jwt_with_oauth_token_routes_to_plan_std(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """JWT + oauth_token → plan_std（zai_plan）渠道优先，免验证码。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()

        acc = seed_account(fresh_app, _JWT_OAUTH, name="oauth-cl")
        acc.oauth_token = "mock-oauth-token"
        fresh_app.update_account(acc)

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        assert res.json()["type"] == "message"
        calls = _messages_calls(mock)
        assert len(calls) == 1
        _, path, headers, _ = calls[0]
        # plan_std routes to /api/anthropic/v1/messages (not zcode-plan proxy)
        assert path == "/api/anthropic/v1/messages"
        # Auth is Bearer oauth_token, not JWT
        assert headers.get("authorization") == "Bearer mock-oauth-token"

    async def test_plan_std_401_falls_to_zcode_plan_not_invalid(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """plan_std（zai_plan）401 → 下沉 zcode_plan（zai），不判死账号。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()
        mock.state.counters.clear()

        acc = seed_account(fresh_app, _JWT_OAUTH, name="oauth-401")
        acc.oauth_token = "stale-oauth-token"
        fresh_app.update_account(acc)

        # plan_std 用 oauth_token Bearer → bind=stale-oauth-toke（16 chars）
        mock.state.sequences[_OAUTH_BIND_STALE] = ["auth_invalid"]
        # zcode_plan 用 JWT Bearer → bind=cl1.eyJzdWIiOiJ（JWT 前 16 chars）
        mock.state.sequences[_JWT_OAUTH[:16]] = ["ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY)

        # zcode_plan 成功
        assert res.status_code == 200
        calls = _messages_calls(mock)
        assert len(calls) == 2, f"expected 2 calls, got {[(c[1], c[2].get('authorization', '')[:60]) for c in calls]}"
        # 第一个 call 走 /api/anthropic (plan_std)，401
        assert calls[0][1] == "/api/anthropic/v1/messages"
        # 第二个 call 走 zcode-plan proxy (zai)，200
        assert calls[1][1] == "/api/v1/zcode-plan/anthropic/v1/messages"
        # 账号不得因 plan_std 401 被标记 INVALID
        assert acc.status != Status.INVALID
        assert acc.status == Status.ACTIVE
        assert acc.use_count == 1

    async def test_plan_std_403_falls_through(self, gateway_client, fresh_app,
                                              stub_captcha):
        """plan_std 403 同样下沉下一渠道，不判死（用 401 模拟——403 同路径）。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()
        mock.state.counters.clear()

        acc = seed_account(fresh_app, _JWT_OAUTH, name="oauth-403")
        acc.oauth_token = "stale-403-token"
        fresh_app.update_account(acc)
        # plan_std bind = oauth_token 前 16 字符
        mock.state.sequences["stale-403-token"] = ["auth_invalid"]
        mock.state.sequences[_JWT_OAUTH[:16]] = ["ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        assert acc.status != Status.INVALID

    async def test_jwt_without_oauth_token_skips_plan_std(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """JWT 无 oauth_token → 直接走 zcode_plan（zai），不试 plan_std。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()

        seed_account(fresh_app, _JWT_ONLY, name="jwt-only")

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        calls = _messages_calls(mock)
        assert len(calls) == 1
        # 只走了 zcode-plan（/zcode-plan/anthropic/v1/messages）
        assert calls[0][1] == "/api/v1/zcode-plan/anthropic/v1/messages"


@pytest.mark.integration
class TestChannelLadderExhaust1310:
    async def test_1310_on_plan_std_marks_exhausted(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """plan_std 1310（周/月使用上限）→ EXHAUSTED，解析重置时间。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()
        mock.state.counters.clear()

        acc = seed_account(fresh_app, _JWT_OAUTH, name="exhaust-1310")
        acc.oauth_token = "mock-1310-token"
        fresh_app.update_account(acc)

        # plan_std bind = oauth_token 前 16 chars
        mock.state.sequences["mock-1310-token"] = ["quota_exhausted_1310"]

        res = await client.post("/v1/messages", json=_MSG_BODY)

        # 唯一账号被标 EXHAUSTED → 503
        assert res.status_code == 503
        assert acc.status == Status.EXHAUSTED
        # 重置时间从 1310 msg 解析："将于 2026-11-15 23:59 重置"
        assert acc.cooling_until is not None
        assert "额度已用完" in (acc.last_error or "")


@pytest.mark.integration
class TestChannelLadderApikeyFallback:
    async def test_jwt_with_api_key_fallthrough_to_apikey(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """JWT + api_key：plan → 401 → 下沉 apikey 成功。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()
        mock.state.counters.clear()

        acc = seed_account(fresh_app, _JWT_ONLY, name="jwt-with-key")
        acc.api_key = "mock-api-key-he"
        fresh_app.update_account(acc)

        # jwt → 401 → api key → 200
        # jwt bind = JWT[:16], api-key bind = api_key[:16]
        mock.state.sequences[_JWT_ONLY[:16]] = ["auth_invalid"]
        mock.state.sequences["mock-api-key-he"] = ["ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        calls = _messages_calls(mock)
        assert len(calls) == 2
        # 第一次：zcode-plan (jwt, 401)
        assert calls[0][1] == "/api/v1/zcode-plan/anthropic/v1/messages"
        # 第二次：api key (zai_fallback, 200)
        assert calls[1][1] == "/api/anthropic/v1/messages"
        assert calls[1][2].get("x-api-key") == "mock-api-key-he"
        # JWT 被标 INVALID（zcode_plan 401 = JWT 失效），但 api key fallback 成功
        assert acc.status == Status.INVALID
        assert acc.use_count == 1
