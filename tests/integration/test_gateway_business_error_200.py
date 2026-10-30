"""HTTP 200 承载业务错误（2026-10-30 线上实测形态）的处置回归。

Plan 通道对额度/风控类拒绝会回 **HTTP 200** + `{"code":1005|3012,"msg":...}`
（content-type application/json，没有 type/content，不是 message 形状）。
网关曾只看状态码，把这类响应当成成功：洗掉 risk_strikes / 冷却 / last_error、
把 EXHAUSTED 拉回 ACTIVE，还把错误体当正文透传给客户端（200 但没有 message）。

这里钉住三条不变量：
  ① 200 + 1005 → 按额度耗尽处置（换号，不算成功、不清风控计数）
  ② 200 + 3012 → 按风控处置（计 strike / 冷却，绝不清零）
  ③ 200 + 合法 message → 仍按成功透传（新判定不得误杀正常响应）
"""

from __future__ import annotations

import pytest

_JWT_QUOTA = "qA.eyJzdWIiOiJxIn0.sig"
_JWT_RISK = "qR.eyJzdWIiOiJyIn0.sig"
_JWT_OK = "qO.eyJzdWIiOiJvIn0.sig"
_MSG_BODY = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def clean_mock(gateway_client):
    client, mock = gateway_client
    mock.state.calls.clear()
    mock.state.counters.clear()
    mock.state.sequences.clear()
    return mock


def _plan_calls(mock) -> list[tuple[str, str, dict, bytes]]:
    """只取 Plan 通道的 messages 请求（billing/claim/usage 也含 zcode-plan 前缀）。"""
    return [c for c in mock.state.calls
            if c[0] == "POST" and c[1].endswith("/anthropic/v1/messages")]


def _last(mock) -> dict:
    return {"calls": _plan_calls(mock), "calls_all": list(mock.state.calls)}


@pytest.mark.integration
class TestBusinessErrorOn200:
    async def test_quota_code_on_200_is_exhausted_not_success(
        self, gateway_client, fresh_app, stub_captcha, clean_mock
    ):
        """200 + code 1005 = 额度耗尽：换下一个号，本号不计成功、不洗风控计数。"""
        from tests.conftest import seed_account

        client, _ = gateway_client
        stub_captcha.needs_captcha = False
        dead = seed_account(fresh_app, _JWT_QUOTA, name="quota-200")
        live = seed_account(fresh_app, _JWT_OK, name="healthy")
        dead.risk_strikes = 2  # 预置计数：若被当成功会被清零
        fresh_app.update_account(dead)

        clean_mock.state.sequences[_JWT_QUOTA[:16]] = ["quota_exhausted_200"]

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        assert res.json()["type"] == "message"
        calls = _plan_calls(clean_mock)
        assert len(calls) == 2, "额度号必须先打一次，再换健康号"
        # 额度号：失败留痕，risk_strikes 不被清零，use_count 未增长
        assert dead.recent_results[-1]["ok"] is False
        assert "额度" in dead.recent_results[-1]["detail"]
        assert dead.risk_strikes == 2
        assert dead.use_count == 0
        # 健康号：正常计数
        assert live.use_count == 1

    async def test_risk_code_on_200_still_counts_as_risk(
        self, gateway_client, fresh_app, stub_captcha, clean_mock
    ):
        """200 + code 3012 = 风控：必须计 strike 并冷却/禁用，绝不当成功洗白。"""
        from tests.conftest import seed_account

        client, _ = gateway_client
        stub_captcha.needs_captcha = False
        acc = seed_account(fresh_app, _JWT_RISK, name="risk-200")

        clean_mock.state.sequences[_JWT_RISK[:16]] = ["risk_control_3012_200"]

        res = await client.post("/v1/messages", json=_MSG_BODY)

        # 唯一账号被风控 → 无可用账号
        assert res.status_code == 503
        assert acc.risk_strikes == 1
        assert acc.status in ("cooling", "disabled")
        assert acc.use_count == 0
        assert acc.recent_results[-1]["ok"] is False
        assert "风控" in acc.recent_results[-1]["detail"]

    async def test_normal_message_on_200_passes_through(
        self, gateway_client, fresh_app, stub_captcha, clean_mock
    ):
        """合法 message 响应（带 type/content）不得被新判定误杀。"""
        from tests.conftest import seed_account

        client, _ = gateway_client
        stub_captcha.needs_captcha = False
        acc = seed_account(fresh_app, _JWT_OK, name="ok-200")

        res = await client.post("/v1/messages", json=_MSG_BODY)

        assert res.status_code == 200
        body = res.json()
        assert body["type"] == "message"
        assert body["content"][0]["text"] == "Hello from mock upstream"
        assert acc.use_count == 1
        assert acc.recent_results[-1]["ok"] is True
