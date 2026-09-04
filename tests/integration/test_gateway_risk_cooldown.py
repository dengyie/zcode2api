"""GW-010 风控冷却（2026-09-05 3012 事件）：命中风控 → 指数退避 + 暂停验证码池。

3012「unusual activity」（HTTP 405 承载）由高频请求触发，是账号级临时风控。
网关若按普通错误回传，下一请求会立刻重打同一账号，只会加剧风控（官方政策：
3 次以上违规可能封号）。因此命中即：
  1. 账号进入指数退避冷却（min(base·2^(n-1), cap) 秒）
  2. 暂停验证码池预热（求解本身就是上游流量）
  3. 切换下一账号；全部风控则 503，不空转
  4. 配额监控冷却期内跳过该账号（billing 也是上游流量，且 200 会提前解除冷却）
"""

from __future__ import annotations

import time

import pytest

from app import settings
from app.models import Status

_RISK_JWT = "hR.eyJzdWIiOiJyIn0.sig"
_GOOD_JWT = "hG.eyJzdWIiOiJnIn0.sig"


@pytest.mark.integration
class TestRiskControlCooldown:
    async def test_3012_triggers_cooldown_and_fails_over(self, gateway_client, fresh_app, stub_captcha):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _RISK_JWT, name="a-risk")
        seed_account(fresh_app, _GOOD_JWT, name="b-good")
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        res = await client.post("/v1/messages", json={"model": "GLM-5.2",
                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 200  # B 正常服务

        accounts = {a.name: a for a in fresh_app.list_accounts("zai")}
        risk = accounts["a-risk"]
        assert risk.status == Status.COOLING
        assert risk.risk_strikes == 1
        assert risk.cooling_until is not None and risk.cooling_until > time.time()
        base = settings.RISK_COOLDOWN_BASE
        assert base * 0.95 <= risk.cooling_until - time.time() <= base  # 首次 ≈ base
        assert "风控" in (risk.last_error or "")
        # 验证码池预热被同步暂停（时长 = 冷却秒数）
        assert stub_captcha.paused_seconds and stub_captcha.paused_seconds[0] == base

    async def test_repeated_hits_back_off_exponentially(self, gateway_client, fresh_app, monkeypatch):
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RISK_COOLDOWN_BASE", 100)
        monkeypatch.setattr(settings, "RISK_COOLDOWN_MAX", 10_000)
        seed_account(fresh_app, _RISK_JWT, name="a-risk")
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        # 冷却过期即可再次被选中 —— 直接清 cooling_until 模拟时间流逝
        def _expire():
            for a in fresh_app.list_accounts("zai"):
                a.cooling_until = time.time() - 1

        res = await client.post("/v1/messages", json={"model": "GLM-5.2",
                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 503  # 唯一账号风控 → 不空转
        acc = fresh_app.list_accounts("zai")[0]
        assert acc.risk_strikes == 1

        _expire()
        res = await client.post("/v1/messages", json={"model": "GLM-5.2",
                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 503
        acc = fresh_app.list_accounts("zai")[0]
        assert acc.risk_strikes == 2
        # 第 2 次冷却 ≈ base·2 = 200s
        assert 190 <= acc.cooling_until - time.time() <= 200

    async def test_cooldown_blocks_selection_until_expiry(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk")
        acc.begin_risk_cooldown(600, 600)
        fresh_app.update_account(acc)

        assert acc.is_selectable() is False
        assert acc.is_cooling() is True

        acc.cooling_until = time.time() - 1
        assert acc.is_selectable() is True
        assert acc.is_cooling() is False

    async def test_success_resets_strikes(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _GOOD_JWT, name="a-good")
        acc.risk_strikes = 3
        fresh_app.update_account(acc)

        res = await client.post("/v1/messages", json={"model": "GLM-5.2",
                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 200
        assert fresh_app.list_accounts("zai")[0].risk_strikes == 0

    async def test_quota_refresh_respects_active_cooldown(self, gateway_client, fresh_app):
        """冷却期内 monitor 不打 billing、fetch_quota 不提前解除冷却。"""
        from app import quota
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk")
        acc.begin_risk_cooldown(600, 600)
        fresh_app.update_account(acc)

        # monitor 的账号筛选跳过冷却中账号
        selected = [a for a in fresh_app.list_accounts("zai")
                    if a.mode == "jwt" and a.status != Status.DISABLED and not a.is_cooling()]
        assert selected == []

        # fetch_quota 拿到额度恢复也不解除风控冷却（兜底分支）
        acc.quota = {}
        await quota.fetch_quota(acc)
        assert acc.status == Status.COOLING
        assert acc.is_cooling() is True

    async def test_captcha_pool_pause_refill(self):
        from app.captcha import CaptchaManager

        mgr = CaptchaManager()
        mgr.pause_refill(30)
        assert mgr._paused_until > 0
        snap = mgr._paused_until
        mgr.pause_refill(10)  # 更短暂停不回退截止
        assert mgr._paused_until == snap
        mgr.pause_refill(60)  # 更长暂停顺延
        assert mgr._paused_until > snap


@pytest.mark.integration
class TestRateLimitRetryAfter:
    """429（官方频控 1302/1313）：尊重上游 Retry-After，缺失/异常才回退默认冷却。"""

    async def test_429_honors_retry_after(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-429")
        mock.state.sequences[_RISK_JWT[:16]] = ["rate_limited"]  # mock 带 retry-after: 30

        res = await client.post("/v1/messages", json={"model": "GLM-5.2",
                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 503  # 唯一账号被限流
        acc = fresh_app.list_accounts("zai")[0]
        assert acc.status == Status.COOLING
        # 冷却 ≈ 30s（Retry-After），而非默认 300s
        assert 25 <= acc.cooling_until - time.time() <= 31
        assert "Retry-After 30s" in (acc.last_error or "")
        # 429 不计入风控连击（它是频控不是 unusual activity 风控）
        assert acc.risk_strikes == 0

    async def test_429_without_header_falls_back(self, gateway_client, fresh_app, monkeypatch):
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        monkeypatch.setattr(gw, "_parse_retry_after", lambda _: None)
        monkeypatch.setattr(settings, "COOLING_SECONDS", 120)
        seed_account(fresh_app, _RISK_JWT, name="a-429")
        mock.state.sequences[_RISK_JWT[:16]] = ["rate_limited"]

        res = await client.post("/v1/messages", json={"model": "GLM-5.2",
                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 503
        acc = fresh_app.list_accounts("zai")[0]
        assert 110 <= acc.cooling_until - time.time() <= 121


def test_parse_retry_after():
    from app.routes.gateway import _parse_retry_after

    assert _parse_retry_after("30") == 30
    assert _parse_retry_after(" 45.0 ") == 45
    assert _parse_retry_after("0") is None       # 非正数不采信
    assert _parse_retry_after("-5") is None
    assert _parse_retry_after("7200") is None    # >1h 封顶防御
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None  # HTTP-date 放弃
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
