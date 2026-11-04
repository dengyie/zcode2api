"""GW-010 上游错误处理策略（2026-09-05 3012/429 事件后定稿，参数见 settings）：

  - 429 频控：账号**不冷却**，原地等待重试（默认 5 次；上游 Retry-After 优先、
    封顶 RETRY_429_WAIT_MAX），耗尽后换下一个账号，账号保持可用
  - 5xx 等一般错误：重试（默认 3 次），耗尽后账号冷却 COOLING_SECONDS 并换号
  - 3012/405「unusual activity」：指数退避冷却（base 900s × 2^(strikes-1)，封顶 24h）；
    累计 4 次升级为 DISABLED（人工确认恢复），避免持续向上游产生流量
  - 冷却期内零**对话**流量：messages 轮询不选、验证码池关门；
    billing 链（preview/claim/查额度）不设冷却闸——领取与使用是两条链
    （2026-11-04 口径：号停着/冷却中也必须能把投放领进账）
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
    async def test_invalid_jwt_falls_back_to_apikey(self, gateway_client, fresh_app):
        """JWT 已失效但同账号有 API Key 时走回退通道，不再 503。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        jwt = "hF.eyJzdWIiOiJmIn0.sig"
        acc = seed_account(fresh_app, jwt, name="a-fb")
        acc.api_key = "sk-fallback-key-aaaa"
        acc.status = Status.INVALID
        acc.last_error = "鉴权失败 HTTP 401"
        fresh_app.update_account(acc)

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        paths = [c[1] for c in mock.state.calls[before:] if c[1].endswith("/messages")]
        assert any(p == "/api/anthropic/v1/messages" for p in paths)
        assert not any("zcode-plan" in p for p in paths)
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.INVALID  # Key 成功不得把废 JWT 洗成 active

    async def test_invalid_jwt_key_5xx_does_not_revive_plan(self, gateway_client, fresh_app, monkeypatch):
        """废 JWT 走 Key 后 5xx 耗尽，不得把 status 洗成 cooling（否则冷却结束会重打 Plan）。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_5XX_TIMES", 1)
        monkeypatch.setattr(settings, "RETRY_5XX_WAIT", 0)
        jwt = "hF.eyJzdWIiOiJmIn0.sig"
        key = "sk-fallback-5xx-aaaa"
        acc = seed_account(fresh_app, jwt, name="a-fb-5xx")
        acc.api_key = key
        acc.status = Status.INVALID
        acc.last_error = "鉴权失败 HTTP 401"
        fresh_app.update_account(acc)
        mock.state.sequences[key[:16]] = ["server_error"]

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.INVALID
        assert after.uses_plan_channel() is False
        assert after.allows_billing() is False
        paths = [c[1] for c in mock.state.calls[before:] if c[1].endswith("/messages")]
        assert not any("zcode-plan" in p for p in paths)

    async def test_invalid_jwt_key_connect_fail_does_not_revive_plan(self, gateway_client, fresh_app):
        """废 JWT 走 Key 后连接失败，不得洗成 cooling。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        jwt = "hF.eyJzdWIiOiJmIn0.sig"
        key = "sk-fallback-conn-bbbb"
        acc = seed_account(fresh_app, jwt, name="a-fb-conn")
        acc.api_key = key
        acc.status = Status.INVALID
        acc.last_error = "鉴权失败 HTTP 401"
        fresh_app.update_account(acc)
        mock.state.sequences[key[:16]] = ["connect_fail_first"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.INVALID
        assert after.uses_plan_channel() is False

    async def test_plan_401_then_key_401_does_not_loop(self, gateway_client, fresh_app):
        """Plan 401 切 Key 后 Key 再 401：换号，不得同账号死循环。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        jwt = "hF.eyJzdWIiOiJmIn0.sig"
        key = "sk-fallback-401-cccc"
        acc = seed_account(fresh_app, jwt, name="a-fb-401")
        acc.api_key = key
        fresh_app.update_account(acc)
        mock.state.sequences[jwt[:16]] = ["auth_invalid"]
        mock.state.sequences[key[:16]] = ["auth_invalid"]

        before = len(mock.state.calls)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.INVALID
        paths = [c[1] for c in mock.state.calls[before:] if c[1].endswith("/messages")]
        assert paths.count("/api/v1/zcode-plan/anthropic/v1/messages") == 1
        assert paths.count("/api/anthropic/v1/messages") == 1

    async def test_3012_falls_back_to_apikey_same_request(self, gateway_client, fresh_app):
        """真风控后进入冷却，同一次请求切 API Key 回退，不 503。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk-fb")
        acc.api_key = "sk-risk-fallback-bbbb"
        fresh_app.update_account(acc)
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        assert acc.status == Status.COOLING
        assert acc.risk_strikes == 1
        assert acc.is_selectable() is False  # 冷却中不可选，但已在本次请求中
        assert acc.cooling_until is not None
        paths = [c[1] for c in mock.state.calls if c[1].endswith("/messages")]
        assert "/api/v1/zcode-plan/anthropic/v1/messages" in paths
        assert "/api/anthropic/v1/messages" in paths

    async def test_pure_apikey_3012_not_selectable(self, gateway_client, fresh_app):
        """纯 API Key 账号 3012 后进入冷却，冷却中不可选。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        key = "sk-pure-risk-key-xxxx"
        acc = seed_account(fresh_app, key, name="a-pure-key")
        mock.state.sequences[key[:16]] = ["risk_control_3012"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.COOLING
        assert after.risk_strikes == 1
        assert after.is_selectable() is False
        assert fresh_app.select("zai") is None

    async def test_pure_apikey_401_marked_invalid(self, gateway_client, fresh_app):
        """纯 API Key 401 应标 INVALID，避免死 Key 永远轮询。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        key = "sk-pure-dead-key-yyyy"
        acc = seed_account(fresh_app, key, name="a-pure-401")
        mock.state.sequences[key[:16]] = ["auth_invalid"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.INVALID
        assert after.is_selectable() is False

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
        assert risk.status == Status.COOLING
        assert risk.enabled is True
        assert risk.risk_strikes == 1
        assert risk.cooling_until is not None
        assert 890 < risk.cooling_until - time.time() <= 905  # ≈ 900s
        assert "指数退避" in (risk.last_error or "")
        assert risk.is_selectable() is False
        assert risk.is_cooling() is True  # 冷却中有恢复倒计时

    async def test_fallback_5xx_preserves_risk_cooldown(self, gateway_client, fresh_app, monkeypatch):
        """风控冷却中回退 Key 5xx 耗尽：不得用 300s 普通冷却覆盖 900s 风控退避
        （gateway 5xx 分支的 is_cooling() 守卫）。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_5XX_TIMES", 1)
        monkeypatch.setattr(settings, "RETRY_5XX_WAIT", 0)
        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk-5xx")
        acc.api_key = "sk-risk-5xx-fb"
        fresh_app.update_account(acc)
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]
        mock.state.sequences["sk-risk-5xx-fb"[:16]] = ["server_error"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 回退通道也 5xx 耗尽
        assert acc.status == Status.COOLING
        # 风控退避（900s）未被普通冷却（300s）覆盖
        assert acc.cooling_until - time.time() > 500
        assert "命中风控" in (acc.last_error or "")
        assert "5xx" not in (acc.last_error or "")
        assert acc.risk_strikes == 1

    async def test_risk_disable_auto_rotates_fingerprint(self, gateway_client, fresh_app, monkeypatch):
        """风控升级禁用时自动换发设备指纹（2.6.8：「风控后换设备重生」自动化），
        重新启用时即全新身份；RISK_AUTO_ROTATE=False 关闭。"""
        client, mock = gateway_client
        from app.fingerprint import profile_for
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RISK_BAN_STRIKES", 1)
        acc = seed_account(fresh_app, _RISK_JWT, name="a-rot")
        old_mid = profile_for(acc).device_mid
        fresh_app.update_account(acc)
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 唯一账号被禁用
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.DISABLED
        assert profile_for(after).device_mid != old_mid  # 已换发全新设备

    async def test_risk_disable_auto_rotate_can_be_disabled(self, gateway_client, fresh_app, monkeypatch):
        client, mock = gateway_client
        from app.fingerprint import profile_for
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RISK_BAN_STRIKES", 1)
        monkeypatch.setattr(settings, "RISK_AUTO_ROTATE", False)
        acc = seed_account(fresh_app, _RISK_JWT, name="a-norot")
        old_mid = profile_for(acc).device_mid
        fresh_app.update_account(acc)
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.DISABLED
        assert profile_for(after).device_mid == old_mid  # 未换发

    async def test_ban_recoverable_by_manual_enable(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk")
        mock.state.sequences[_RISK_JWT[:16]] = ["risk_control_3012"]

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 唯一账号被冷却 → 不空转
        assert acc.status == Status.COOLING

        fresh_app.set_enabled("zai", acc.id, True)  # 人工确认恢复后启用
        assert acc.status == Status.ACTIVE
        assert acc.is_selectable() is True
        assert acc.risk_strikes == 1  # 人工启用不清 strikes（仅成功请求清零）

        # 冷却窗口结束后清除状态模拟自然恢复，再次请求应成功
        acc.risk_strikes = 0
        fresh_app.update_account(acc)
        mock.state.sequences[_RISK_JWT[:16]] = ["ok"]
        res2 = await client.post("/v1/messages", json=_MSG_BODY)
        assert res2.status_code == 200

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
        assert "指数退避" in (acc.last_error or "")  # 第 2 次仍未升级禁用


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

    async def test_plan_429_exhausted_falls_back_to_key_budget(self, gateway_client, fresh_app, monkeypatch):
        """Plan 通道 429 烧完预算后，回退 Key 通道应有自己的独立预算，
        不得直接换号把回退饿死（每次请求两通道各享 RETRY_429_TIMES 次重试）。"""
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_TIMES", 2)
        monkeypatch.setattr(settings, "RETRY_429_WAIT", 0)
        monkeypatch.setattr(gw, "_parse_retry_after", lambda _: None)

        jwt = "h4.eyJzdWIiOiI0In0.sig"
        key = "sk-fallback-429-dddd"
        acc = seed_account(fresh_app, jwt, name="a-429-fb")
        acc.api_key = key
        fresh_app.update_account(acc)
        mock.state.sequences[jwt[:16]] = ["rate_limited"]
        mock.state.sequences[key[:16]] = ["rate_limited"]

        bind_jwt, bind_key = jwt[:16], key[:16]
        before_jwt = mock.state.counters.get(bind_jwt, 0)
        before_key = mock.state.counters.get(bind_key, 0)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        # 两通道各 1 + 2 次重试 = 各 3 次打到上游后才换号 → 503（唯一账号）
        assert res.status_code == 503
        assert mock.state.counters.get(bind_jwt, 0) - before_jwt == 3
        assert mock.state.counters.get(bind_key, 0) - before_key == 3
        # 429 不是账号故障：通道状态不得被污染
        after = fresh_app.find("zai", acc.id)
        assert after.status == Status.ACTIVE
        assert acc.status == Status.ACTIVE

    async def test_429_with_quota_body_is_not_exhausted(self, gateway_client, fresh_app, monkeypatch):
        """线上 2026-09-13：429 body 带 quota/额度等关键词，被 _is_exhausted
        关键词分支抢先判成「额度用完」→ 账号被误标 EXHAUSTED 踢出轮换。
        429 必须优先走频控重试路径，账号保持可用。"""
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_TIMES", 2)
        monkeypatch.setattr(settings, "RETRY_429_WAIT", 0)
        monkeypatch.setattr(gw, "_parse_retry_after", lambda _: None)
        seed_account(fresh_app, _RISK_JWT, name="a-429-quota-body")
        mock.state.sequences[_RISK_JWT[:16]] = ["rate_limited_quota_body"]

        bind = _RISK_JWT[:16]
        before = mock.state.counters.get(bind, 0)
        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 503  # 重试耗尽且无其它账号
        assert mock.state.counters.get(bind, 0) - before == 3  # 1 + 2 次频控重试
        acc = fresh_app.list_accounts("zai")[0]
        assert acc.status == Status.ACTIVE  # 不得误标 EXHAUSTED
        assert acc.is_selectable() is True

    async def test_429_wait_uses_capped_retry_after(self, gateway_client, fresh_app, monkeypatch):
        client, mock = gateway_client
        from app.routes import gateway as gw
        from tests.conftest import seed_account

        monkeypatch.setattr(settings, "RETRY_429_TIMES", 2)
        monkeypatch.setattr(settings, "RETRY_429_WAIT_MAX", 10)  # mock 带 retry-after: 30
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(gw, "_sleep", fake_sleep)
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
        acc.last_risk_at = time.time() - 60
        acc.status = Status.COOLING
        acc.cooling_until = time.time() - 1  # 已过期才可选；成功后应彻底清理
        acc.last_error = "风控封禁 (3012/unusual activity) HTTP 405，确认恢复后请在后台手动启用（第 3 次）"
        fresh_app.update_account(acc)

        res = await client.post("/v1/messages", json=_MSG_BODY)
        assert res.status_code == 200
        after = fresh_app.list_accounts("zai")[0]
        assert after.risk_strikes == 0
        assert after.last_risk_at is None
        assert after.status == Status.ACTIVE
        assert after.last_error is None
        assert after.cooling_until is None

    async def test_quota_refresh_includes_cooling_billing_chain(self, gateway_client, fresh_app):
        """冷却期 billing 照查（领取/监控与对话是两条链），但 fetch_quota 不提前解除冷却。"""
        from app import quota
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-risk")
        _set_cooling(acc)
        fresh_app.update_account(acc)

        # monitor 的账号筛选包含冷却中账号（billing 链不设冷却闸）
        selected = [a for a in fresh_app.list_accounts("zai")
                    if a.mode == "jwt" and a.allows_billing()]
        assert [a.id for a in selected] == [acc.id]

        # fetch_quota 拿到额度恢复也不解除冷却（兜底分支）
        acc.quota = {}
        await quota.fetch_quota(acc)
        assert acc.status == Status.COOLING
        assert acc.is_cooling() is True

    async def test_fetch_quota_skips_invalid_but_not_disabled(self, gateway_client, fresh_app):
        """只有废 JWT 不打 billing；风控禁用 / 人工停用照查，且额度数字不得改状态。"""
        from app import quota
        from tests.conftest import seed_account

        _client, mock = gateway_client
        acc = seed_account(fresh_app, _RISK_JWT, name="a-dead-q")
        acc.status = Status.INVALID
        acc.last_error = "鉴权失败 HTTP 401"
        fresh_app.update_account(acc)

        before = len(mock.state.calls)
        res = await quota.fetch_quota(acc)
        assert "error" in res
        assert not [c for c in mock.state.calls[before:] if "/billing" in c[1]]
        assert acc.status == Status.INVALID

        # 风控禁用：billing 照查，状态绝不被额度数字洗掉（防 DISABLED→EXHAUSTED→ACTIVE）
        acc.status = Status.DISABLED
        acc.enabled = True
        fresh_app.update_account(acc)
        before = len(mock.state.calls)
        res = await quota.fetch_quota(acc)
        assert "error" not in res
        assert [c for c in mock.state.calls[before:] if "/billing" in c[1]]
        assert acc.status == Status.DISABLED

        # 人工停放（enabled=False）：照查，仍然不可选
        acc.status = Status.ACTIVE
        acc.enabled = False
        fresh_app.update_account(acc)
        before = len(mock.state.calls)
        res = await quota.fetch_quota(acc)
        assert "error" not in res
        assert [c for c in mock.state.calls[before:] if "/billing" in c[1]]
        assert acc.is_selectable() is False

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

        acc.api_key = "sk-fallback-still-jwt-dead"
        acc.status = Status.INVALID
        fresh_app.update_account(acc)
        assert mgr._gate_open() is False  # JWT 失效即使有 Key 回退也不预热验证码

        acc.api_key = None
        acc.status = Status.EXHAUSTED
        acc.cooling_until = None
        fresh_app.update_account(acc)
        assert mgr._gate_open() is False  # 额度用完不可对话，不预热验证码


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
class TestAdminBillingChainIgnoresCooldown:
    """后台 billing 路径（刷新/领取）不设冷却闸：只有废 JWT / 无 JWT 被拦。

    领取与使用两条链（2026-11-04 口径）：号停着或冷却中照样要能查额度、领投放；
    "冷却期零上游流量" 只约束 messages 通道。
    """

    @pytest.fixture
    def claim_spy(self, monkeypatch):
        """替换 admin 侧的 do_claim，只验证门控放行（不污染 mock 的 claim 调用账本）。"""
        from app.routes import admin_api

        calls: list[str] = []

        async def fake_do_claim(acc, plan_id=None):
            calls.append(acc.id)
            return {"plan_id": "mock-claim-plan", "plan_name": "Mock Daily Plan",
                    "grants": []}

        monkeypatch.setattr(admin_api, "do_claim", fake_do_claim)
        return calls

    async def test_refresh_all_includes_cooling(self, gateway_client, fresh_app):
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
        assert body["count"] == 1
        assert body["cooling"] == 1
        assert body["skipped_invalid"] == 0
        assert [c for c in mock.state.calls[before:] if "/billing" in c[1]]  # 照查

    async def test_refresh_one_works_for_cooling(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-cool")
        _set_cooling(acc)
        fresh_app.update_account(acc)

        res = await client.post(f"/admin/api/accounts/{acc.id}/refresh", headers=ADMIN_AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert "冷却" not in body.get("message", "")
        # 冷却态保持（billing 成功不得洗成 active）
        assert fresh_app.find("zai", acc.id).status == Status.COOLING

    async def test_refresh_all_skips_invalid(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-invalid")
        acc.status = Status.INVALID
        acc.last_error = "鉴权失败 HTTP 401"
        fresh_app.update_account(acc)

        before = len(mock.state.calls)
        res = await client.post("/admin/api/accounts/refresh",
                                json={"all": True}, headers=ADMIN_AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 0
        assert body["skipped_invalid"] == 1
        assert not [c for c in mock.state.calls[before:] if "/billing" in c[1]]

    async def test_claim_reaches_cooling_account(self, gateway_client, fresh_app,
                                                 claim_spy):
        """冷却号照领：旧版会回「冷却中，已跳过」，现在必须真的走到 do_claim。"""
        client, _mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _RISK_JWT, name="a-cool")
        _set_cooling(acc)
        fresh_app.update_account(acc)

        res = await client.post("/admin/api/claim",
                                json={"account_ids": [acc.id]}, headers=ADMIN_AUTH)
        assert res.status_code == 200
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is True, outcome
        assert "冷却" not in outcome.get("message", "")
        assert claim_spy == [acc.id]

    async def test_claim_reaches_parked_account(self, gateway_client, fresh_app,
                                                claim_spy):
        """人工停放（enabled=false / DISABLED）号照领。"""
        client, _mock = gateway_client
        from tests.conftest import seed_account

        acc = seed_account(fresh_app, _GOOD_JWT, name="a-parked")
        acc.enabled = False
        acc.status = Status.DISABLED
        fresh_app.update_account(acc)

        res = await client.post("/admin/api/claim",
                                json={"account_ids": [acc.id]}, headers=ADMIN_AUTH)
        outcome = res.json()["outcomes"][0]
        assert outcome["ok"] is True, outcome
        assert claim_spy == [acc.id]

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
        assert body["cooling"] == 0
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
