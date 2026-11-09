"""渠道优先梯（zcode_plan → apikey）集成测试。

JWT 账号走 zcode-plan 通道（zcode.z.ai 代理）；plan 通道 401 时下沉 API Key
回退；1310 额度耗尽写入重置时间戳。oauth_token 仅作信息留存，不得改变路由。
"""

from __future__ import annotations

import pytest

from app.models import Status

_JWT_OAUTH = "cl1.eyJzdWIiOiJjbCJ9.sig"
_JWT_ONLY = "cl2.eyJzdWIiOiJjbDIifQ.sig"
_MSG_BODY = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}]}


def _messages_calls(mock) -> list:
    return [c for c in mock.state.calls
            if c[0] == "POST" and c[1].endswith("/anthropic/v1/messages")]


@pytest.mark.integration
class TestChannelLadderZcodePlan:
    async def test_jwt_routes_to_zcode_plan(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """JWT 账号 → zcode-plan（zai）通道，免验证码。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()

        seed_account(fresh_app, _JWT_ONLY, name="jwt-plain")

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        assert res.json()["type"] == "message"
        calls = _messages_calls(mock)
        assert len(calls) == 1
        _, path, headers, _ = calls[0]
        assert path == "/api/v1/zcode-plan/anthropic/v1/messages"
        assert headers.get("authorization") == f"Bearer {_JWT_ONLY}"

    async def test_oauth_token_does_not_hijack_routing(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """回归：oauth_token 仅信息留存，JWT 仍走 zcode-plan（不是标准端点）。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()

        acc = seed_account(fresh_app, _JWT_OAUTH, name="oauth-flag")
        acc.oauth_token = "mock-oauth-token"
        fresh_app.update_account(acc)

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        calls = _messages_calls(mock)
        assert len(calls) == 1
        _, path, headers, _ = calls[0]
        # 仍走 zcode-plan 代理，而非 /api/anthropic（api.z.ai 标准端点）
        assert path == "/api/v1/zcode-plan/anthropic/v1/messages"
        assert headers.get("authorization") == f"Bearer {_JWT_OAUTH}"


@pytest.mark.integration
class TestChannelLadderExhaust1310:
    async def test_1310_on_zcode_plan_marks_exhausted(
        self, gateway_client, fresh_app, stub_captcha
    ):
        """zcode-plan 1310（周/月使用上限）→ EXHAUSTED，解析重置时间。"""
        from tests.conftest import seed_account

        client, mock = gateway_client
        stub_captcha.needs_captcha = False
        mock.state.calls.clear()
        mock.state.counters.clear()

        acc = seed_account(fresh_app, _JWT_OAUTH, name="exhaust-1310")

        # zcode-plan 用 JWT Bearer → bind=JWT 前 16 chars
        mock.state.sequences[_JWT_OAUTH[:16]] = ["quota_exhausted_1310"]

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
