"""验证码「按上游配置决定带不带」（2.7.1）。

官方 bundle：`mode==="start-plan" && enabled!==false && skipModelRequest!==true`
才在模型请求上带 `X-Aliyun-Captcha-Verify-Param`。线上 client/configs 实测
`skip_model_request:true` ⇒ 官方客户端模型请求**不带**验证码头；hub 曾无条件
带，属于「官方不会做的事」（3012 结构性嫌疑）。

策略：配置说不需要就不带；上游一旦明确挑战（3007/响应头/文案），下一趟补码
重试——既对齐官方形态，又保留对「配置与实际不符」的自愈能力。
"""

from __future__ import annotations

import pytest

_JWT_SKIP = "hA.eyJzdWIiOiJhIn0.sig"
_JWT_BACKFILL = "hB.eyJzdWIiOiJiIn0.sig"
_JWT_REQUIRE = "hC.eyJzdWIiOiJjIn0.sig"
_MSG_BODY = {"model": "GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}
_CAPTCHA_HEADER = "x-aliyun-captcha-verify-param"  # Mock 侧 headers 键为小写


@pytest.fixture
def clean_mock(gateway_client):
    """Mock 是 session 级：每个用例前清空调用记录/计数/场景队列，避免互相污染。"""
    _client, mock = gateway_client
    mock.state.calls.clear()
    mock.state.counters.clear()
    mock.state.sequences.clear()
    return mock


def _plan_calls(mock) -> list[tuple[str, str, dict, bytes]]:
    """只取打到 Plan 通道的请求（回退通道是 api/anthropic，另算）。"""
    return [c for c in mock.state.calls if "zcode-plan" in c[1]]


@pytest.mark.integration
class TestSkipModelRequest:
    async def test_no_captcha_header_when_config_skips_it(
        self, gateway_client, fresh_app, stub_captcha, clean_mock
    ):
        """skip_model_request=true ⇒ 首探不带码，也不为此付出求解成本。"""
        client, _ = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _JWT_SKIP, name="skip-a")
        stub_captcha.needs_captcha = False

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        assert stub_captcha.solve_count == 0
        calls = _plan_calls(clean_mock)
        assert calls and all(_CAPTCHA_HEADER not in h for _, _, h, _ in calls)

    async def test_challenge_without_captcha_triggers_backfill_retry(
        self, gateway_client, fresh_app, stub_captcha, clean_mock
    ):
        """配置说不用，但上游 3007 要码 → 同账号补码重试并成功。"""
        client, _ = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _JWT_BACKFILL, name="skip-b")
        stub_captcha.needs_captcha = False
        clean_mock.state.sequences[_JWT_BACKFILL[:16]] = ["captcha_3007", "ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        assert stub_captcha.solve_count == 1
        assert stub_captcha.invalidated == 1  # 挑战后清池，避免复用被判废的 token
        calls = _plan_calls(clean_mock)
        assert len(calls) == 2
        assert _CAPTCHA_HEADER not in calls[0][2]  # 首探不带
        assert calls[1][2].get(_CAPTCHA_HEADER) == "mock-verify-param"  # 补码后带

    async def test_captcha_header_sent_when_config_requires_it(
        self, gateway_client, fresh_app, stub_captcha, clean_mock
    ):
        """skip_model_request=false ⇒ 恢复老行为：首探就带码。"""
        client, _ = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _JWT_REQUIRE, name="skip-c")
        stub_captcha.needs_captcha = True

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        assert stub_captcha.solve_count == 1
        calls = _plan_calls(clean_mock)
        assert calls[0][2].get(_CAPTCHA_HEADER) == "mock-verify-param"
