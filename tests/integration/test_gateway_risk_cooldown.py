"""GW-010 上游错误处理策略（2026-09-05 3012/429 事件后定稿，参数见 settings）：

  - 429 频控：账号**不冷却**，原地等待重试（默认 5 次；上游 Retry-After 优先、
    封顶 RETRY_429_WAIT_MAX），耗尽后换下一个账号，账号保持可用
  - 5xx 等一般错误：重试（默认 3 次），耗尽后账号冷却 COOLING_SECONDS 并换号
  - 3012/405「unusual activity」真风控：**直接禁用账号**（UI 展示封禁文案），
    人工确认恢复后手动启用，不做自动退避——避免对封禁账号持续产生上游流量
  - 冷却期内零上游流量：monitor 跳过、后台手动路径跳过、验证码池关门
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import settings
from app.models import Status

ADMIN_AUTH = {"Authorization": "Bearer zcode"}  # 默认后台密钥

_RISK_JWT = "hR.eyJzdWIiOiJyIn0.sig"
_GOOD_JWT = "hG.eyJzdWIiOiJnIn0.sig"

_MSG_BODY = {"model": "GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}


def _set_cooling(acc, seconds: float = 600.0) -> None:
    """直接置冷却态（5xx 耗尽 / 连接失败走的就是这条路）。"""
    acc.status = Status.COOLING
    acc.cooling_until = time.time() + seconds


@pytest.mark.integration
class TestRiskControlBan:
    async def test_3012_bans_account_and_fails_over(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _RISK_JWT, name="a-risk")
        seed_account(fresh_app, _GOOD_JWT, name="b-good")
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200  # B 正常服务

        accounts = {a.name: a for a in fresh_app.list_accounts("zai")}
        risk = accounts["a-risk"]
        assert risk.status == Status.DISABLED
        assert risk.enabled is True  # 便于后台一键重新启用
        assert risk.risk_strikes == 1
        assert risk.cooling_until is None
        assert "风控封禁" in (risk.last_error or "")
        assert risk.is_selectable() is False
        assert risk.is_cooling() is False  # 封禁不是冷却，不计恢复倒计时

    async def test_ban_recoverable_by_manual_enable(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk")
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 唯一账号被封 → 不空转
        assert acc.status == Status.DISABLED

        fresh_app.set_enabled("zai", acc.id, True)  # 人工确认恢复后启用
        assert acc.status == Status.ACTIVE
        assert acc.is_selectable() is True

    async def test_repeated_bans_accumulate(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk")
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        await client.post("/v1/messages", json=_MSG_BODY)
        assert acc.risk_strikes == 1
        fresh_app.set_enabled("zai", acc.id, True)
        await client.post("/v1/messages", json=_MSG_BODY)
        assert acc.risk_strikes == 2
        assert "第 2 次" in (acc.last_error or "")


@pytest.mark.integration
class Test429Retry:
    """429 不进冷却：原地重试，耗尽后换号且账号保持可用。"""

    async def test_429_retries_then_fails_over_account_stays_active(
        self, gateway_client, fresh_app, monkeypatch
    ):
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_TIMES", 2)
        monkeypatch.setattr(settings, "RETRY_429_WAIT", 0)
        monkeypatch.setattr(gw, "_parse_retry_after", lambda _: None)  # mock 自带 30s，屏蔽掉免睡
        acc = seed_account(fresh_app, _RISK_JWT, name="a-429")
        mock.state.sequences[_RISK_JWT[:16]] = ["rate_limited"]

        bind = _RISK_JWT[:16]
        before = mock.state.counters.get(bind, 0)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 重试耗尽且无其它账号
        # 上游被调 1 + 2 次重试 = 3
        assert mock.state.counters.get(bind, 0) - before == 3
        # 账号保持可用：未冷却、未计封禁
        acc = fresh_app.list_accounts("zai")[0]
        assert acc.status == Status.ACTIVE
        assert acc.cooling_until is None
        assert acc.risk_strikes == 0
        assert acc.is_selectable() is True

    async def test_429_retry_then_success(self, gateway_client, fresh_app, monkeypatch):
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_TIMES", 2)
        monkeypatch.setattr(settings, "RETRY_429_WAIT", 0)
        monkeypatch.setattr(gw, "_parse_retry_after", lambda _: None)
        seed_account(fresh_app, _RISK_JWT, name="a-429")
        mock.state.sequences[_RISK_JWT[:16]] = ["rate_limited", "ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200  # 第 1 次重试即成功
        acc = fresh_app.list_accounts("zai")[0]
        assert acc.use_count == 1
        assert acc.status == Status.ACTIVE

    async def test_429_wait_uses_capped_retry_after(self, gateway_client, fresh_app, monkeypatch):
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_TIMES", 2)
        monkeypatch.setattr(settings, "RETRY_429_WAIT_MAX", 10)  # mock 带 retry-after: 30
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(gw.asyncio, "sleep", fake_sleep)
        seed_account(fresh_app, _RISK_JWT, name="a-429")
        mock.state.sequences[_RISK_JWT[:16]] = ["rate_limited"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        assert slept == [10, 10]  # Retry-After 30 被封顶采信为 10


@pytest.mark.integration
class Test5xxRetry:
    """5xx 等一般错误：重试，耗尽才进冷却。"""

    async def test_5xx_retries_then_cools(self, gateway_client, fresh_app, monkeypatch):
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_5XX_TIMES", 2)
        monkeypatch.setattr(settings, "RETRY_5XX_WAIT", 0)
        acc = seed_account(fresh_app, _RISK_JWT, name="a-5xx")
        mock.state.sequences[_RISK_JWT[:16]] = ["server_error"]

        bind = _RISK_JWT[:16]
        before = mock.state.counters.get(bind, 0)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        assert mock.state.counters.get(bind, 0) - before == 3  # 1 + 2 次重试
        acc = fresh_app.list_accounts("zai")[0]
        assert acc.status == Status.COOLING
        assert acc.cooling_until is not None
        assert 0 < acc.cooling_until - time.time() <= settings.COOLING_SECONDS
        assert "重试 2 次耗尽" in (acc.last_error or "")
        assert acc.risk_strikes == 0  # 5xx 不计风控封禁

    async def test_5xx_retry_then_success(self, gateway_client, fresh_app, monkeypatch):
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_5XX_TIMES", 3)
        monkeypatch.setattr(settings, "RETRY_5XX_WAIT", 0)
        seed_account(fresh_app, _RISK_JWT, name="a-5xx")
        mock.state.sequences[_RISK_JWT[:16]] = ["server_error", "ok"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        acc = fresh_app.list_accounts("zai")[0]
        assert acc.status == Status.ACTIVE
        assert acc.cooling_until is None


@pytest.mark.integration
class TestCooldownBehavior:
    async def test_cooldown_blocks_selection_until_expiry(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk")
        _set_cooling(acc)

        assert acc.is_selectable() is False
        assert acc.is_cooling() is True

        acc.cooling_until = time.time() - 1
        assert acc.is_selectable() is True
        assert acc.is_cooling() is False

    async def test_success_resets_strikes_and_cleans_state(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _GOOD_JWT, name="a-good")
        acc.risk_strikes = 3
        acc.status = Status.COOLING
        acc.cooling_until = time.time() - 1  # 已过期才可选；成功后应彻底清理
        acc.last_error = "风控封禁 (3012/unusual activity) HTTP 405，确认恢复后请在后台手动启用（第 3 次）"
        fresh_app.update_account(acc)

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        after = fresh_app.list_accounts("zai")[0]
        assert after.risk_strikes == 0
        assert after.status == Status.ACTIVE
        assert after.last_error is None
        assert after.cooling_until is None

    async def test_quota_refresh_respects_active_cooldown(self, gateway_client, fresh_app):
        """冷却期内 monitor 不打 billing、fetch_quota 不提前解除冷却。"""
        from app import quota
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk")
        _set_cooling(acc)
        fresh_app.update_account(acc)

        # monitor 的账号筛选跳过冷却中账号
        selected = [a for a in fresh_app.list_accounts("zai")
                    if a.mode == "jwt" and a.status != Status.DISABLED and not a.is_cooling()]
        assert selected == []

        # fetch_quota 拿到额度恢复也不解除冷却（兜底分支）
        acc.quota = {}
        await quota.fetch_quota(acc)
        assert acc.status == Status.COOLING
        assert acc.is_cooling() is True

    async def test_captcha_refill_gate_follows_account_state(self, fresh_app):
        """refill 门控：存在可选 jwt 账号才预热；全冷却/封禁（含重启后，状态已落库）关门。"""
        from app.captcha import CaptchaManager
        from tests.conftest import seed_account

        mgr = CaptchaManager()
        assert mgr._gate_open() is False  # 无账号不预热
        acc = seed_account(fresh_app, _RISK_JWT, name="a-gate")
        assert mgr._gate_open() is True

        _set_cooling(acc)
        fresh_app.update_account(acc)
        assert mgr._gate_open() is False  # 全冷却 → 关门

        acc.status = Status.DISABLED  # 风控封禁态
        acc.cooling_until = None
        fresh_app.update_account(acc)
        assert mgr._gate_open() is False  # 封禁同样关门


@pytest.mark.integration
class TestBillingRefreshDebounce:
    """成功对话后的计费刷新去抖：每条消息都刷 billing 是流量放大器（review P2）。"""

    async def test_success_refresh_debounced(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="a-deb")
        body = {"model": "GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}

        before = len(mock.state.calls)
        res1 = await client.post("/v1/messages", json=body)
        assert res1.status_code == 200
        await asyncio.sleep(0.3)  # 等 _safe_refresh 后台任务跑完
        first = len([c for c in mock.state.calls[before:] if "/billing" in c[1]])
        assert first >= 2  # 首刷发生（current+balance；usage 路径不含 /billing）

        res2 = await client.post("/v1/messages", json=body)
        assert res2.status_code == 200
        await asyncio.sleep(0.3)
        second = len([c for c in mock.state.calls[before:] if "/billing" in c[1]])
        assert second == first  # 60s 去抖窗口内不重刷

        # 去抖窗口过后（模拟时间流逝）恢复刷新
        for a in fresh_app.list_accounts("zai"):
            a.last_checked_at = time.time() - settings.BILLING_REFRESH_MIN_INTERVAL - 1
        res3 = await client.post("/v1/messages", json=body)
        assert res3.status_code == 200
        await asyncio.sleep(0.3)
        third = len([c for c in mock.state.calls[before:] if "/billing" in c[1]])
        assert third >= first + 2


@pytest.mark.integration
class TestAdminEndpointsRespectCooldown:
    """后台手动路径与 QuotaMonitor 同一不变量：冷却期零上游流量。"""

    async def test_refresh_all_skips_cooling(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-cool")
        _set_cooling(acc)
        fresh_app.update_account(acc)

        before = len(mock.state.calls)
        res = await client.post("/admin/api/accounts/refresh",
                                json={"all": True}, headers=ADMIN_AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 0
        assert body["skipped_cooling"] == 1
        assert not [c for c in mock.state.calls[before:] if "/billing" in c[1]]

    async def test_refresh_one_skips_cooling(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-cool")
        _set_cooling(acc)
        fresh_app.update_account(acc)

        res = await client.post(f"/admin/api/accounts/{acc.id}/refresh", headers=ADMIN_AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert "冷却" in body["message"]

    async def test_claim_skips_cooling(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-cool")
        _set_cooling(acc)
        fresh_app.update_account(acc)

        before = len(mock.state.calls)
        res = await client.post("/admin/api/claim", json={}, headers=ADMIN_AUTH)
        assert res.status_code == 200
        outcomes = res.json()["outcomes"]
        assert len(outcomes) == 1
        assert outcomes[0]["ok"] is False
        assert "冷却" in outcomes[0]["message"]
        # billing/claim 未被打到
        assert not [c for c in mock.state.calls[before:] if "/billing" in c[1]]

    async def test_refresh_still_works_for_active_accounts(self, gateway_client, fresh_app):
        """非冷却账号不受门控影响（防修过头）。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="a-active")
        res = await client.post("/admin/api/accounts/refresh",
                                json={"all": True}, headers=ADMIN_AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 1
        assert body["skipped_cooling"] == 0
        assert body["summary"]["ok"] == 1


def test_parse_retry_after():
    from app.routes.gateway import _parse_retry_after

    assert _parse_retry_after("30") == 30
    assert _parse_retry_after(" 45.0 ") == 45
    assert _parse_retry_after("0") is None       # 非正数不采信
    assert _parse_retry_after("-5") is None
    cap = settings.RETRY_429_WAIT_MAX
    assert _parse_retry_after("7200") == cap     # 长值封顶采信（防吊死客户端）
    assert _parse_retry_after("999999") == cap
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None  # HTTP-date 放弃
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
