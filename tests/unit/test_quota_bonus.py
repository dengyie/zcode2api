"""quota 额度耗尽判定的赠送池逻辑单元测试（GW-013 / 2026-09-06 数据审计）。

背景：billing/balance 只含周期性日窗口，不含 one_time 赠送池。旧判定在日窗口
耗尽时把账号标 EXHAUSTED → store.select 跳过 → 请求 503，而赠送池仍有 3 亿级
可用额度。修复后赠送池有效时不判耗尽。

_bonus_active 为纯函数，直接覆盖各时间/period 形态。
"""

from __future__ import annotations

from app.quota import _bonus_active

_NOW = 1_000_000.0


def _plan(period="one_time", eff=None, ends=None):
    ent = {"period": period, "effective_at": _NOW - 100 if eff is None else eff}
    if ends is not None:
        ent["ends_at"] = ends
    return {"entitlements": [ent]}


class TestBonusActive:
    def test_active_bonus(self):
        assert _bonus_active(_plan(), _NOW) is True

    def test_no_end_means_open_ended(self):
        assert _bonus_active(_plan(ends=None), _NOW) is True

    def test_future_effective_not_active(self):
        assert _bonus_active(_plan(eff=_NOW + 100), _NOW) is False

    def test_expired_bonus_not_active(self):
        assert _bonus_active(_plan(eff=_NOW - 200, ends=_NOW - 100), _NOW) is False

    def test_expired_at_boundary(self):
        # ends == now 视为已结束（now <= ends 时有效）
        assert _bonus_active(_plan(eff=_NOW - 200, ends=_NOW), _NOW) is True

    def test_recurring_period_ignored(self):
        assert _bonus_active(_plan(period="daily"), _NOW) is False

    def test_empty_or_malformed_plan(self):
        assert _bonus_active({}, _NOW) is False
        assert _bonus_active({"entitlements": []}, _NOW) is False
        assert _bonus_active({"entitlements": [{"period": "one_time"}]}, _NOW) is False
        assert _bonus_active(None, _NOW) is False

    def test_expired_plan_kills_open_ended_entitlement(self):
        """套餐到期后，不带 ends_at 的 one_time 授权随套餐一起失效（2.7.64）。

        线上形态：zcode-v3-start-plan-1116 于 11-17 17:00 到期，entitlement 只
        带 effective_at → 旧判定永久 has_bonus=True，账号永不判 EXHAUSTED。
        """
        plan = _plan(ends=None)
        plan["ends_at"] = _NOW - 1
        assert _bonus_active(plan, _NOW) is False

    def test_live_plan_keeps_open_ended_entitlement(self):
        plan = _plan(ends=None)
        plan["ends_at"] = _NOW + 3600
        assert _bonus_active(plan, _NOW) is True

    def test_entitlement_end_capped_by_plan_end(self):
        """授权自带更晚的 ends_at 也不得越过套餐有效期。"""
        plan = _plan(ends=_NOW + 10_000)
        plan["ends_at"] = _NOW - 1
        assert _bonus_active(plan, _NOW) is False
