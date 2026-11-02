"""领取窗口调度（2.7.10）：北京时间小时窗口 + store 解析。

上游套餐按天投放（用户侧实证：每天 19:00 放量）。窗口必须锚定**北京时钟**
（host 换成 UTC 也不漂），中国无夏令时，`gmtime(t + 8*3600)` 即精确。
"""

from __future__ import annotations

import calendar

import pytest

from app import settings
from app.claim import ClaimRoundManager
from app.store import Store


def _utc(*args) -> float:
    """UTC 元组 → epoch（窗口数学全部用显式 UTC 时刻锚定北京时间）。"""
    return calendar.timegm(args)


class TestWindowMath:
    # UTC 时刻 → 北京 = UTC+8：11:00 UTC = 北京 19:00，10:00 = 18:00，12:00 = 20:00，15:00 = 23:00
    BJ_19 = _utc(2026, 11, 3, 11, 0, 0)
    BJ_18 = _utc(2026, 11, 3, 10, 0, 0)
    BJ_20 = _utc(2026, 11, 3, 12, 0, 0)
    BJ_23 = _utc(2026, 11, 3, 15, 0, 0)

    def test_beijing_hour_frac_anchored_to_utc8(self):
        assert ClaimRoundManager._beijing_hour_frac(self.BJ_20) == 20.0
        assert ClaimRoundManager._beijing_hour_frac(self.BJ_19) == 19.0
        # 23:30 UTC → 北京次日 07:30（跨天不漂）
        assert ClaimRoundManager._beijing_hour_frac(_utc(2026, 11, 3, 23, 30, 0)) == pytest.approx(7.5)

    def test_in_window(self):
        assert ClaimRoundManager._in_window([19], self.BJ_19) is True
        assert ClaimRoundManager._in_window([], self.BJ_20) is True  # 空窗口 = 全天
        assert ClaimRoundManager._in_window([19], self.BJ_18) is False  # 北京 18:00
        assert ClaimRoundManager._in_window([19, 20, 21], self.BJ_20) is True

    def test_seconds_until_window(self):
        assert ClaimRoundManager._seconds_until_window([19], self.BJ_18) == pytest.approx(3600.0)  # 18→19 整 1h
        assert ClaimRoundManager._seconds_until_window([19], self.BJ_19) == 0.0  # 窗口内
        assert ClaimRoundManager._seconds_until_window([19], self.BJ_23) == pytest.approx(20 * 3600.0)  # 今天已过 → 明天 19:00
        # 多窗口取最近：北京 20:00、窗口 [21,19] → 距 21:00 整 1h
        assert ClaimRoundManager._seconds_until_window([21, 19], self.BJ_20) == pytest.approx(3600.0)

    def test_next_wait(self):
        assert ClaimRoundManager._next_wait(0, [19], self.BJ_20) == 30.0  # 关闭态回看
        assert ClaimRoundManager._next_wait(1800, [], self.BJ_20) == 1800.0  # 无窗口 = 纯节拍
        assert ClaimRoundManager._next_wait(1800, [19], self.BJ_19) == 1800.0  # 窗口内 = 节拍
        # 窗口远（北京 10:00 → 19:00 还有 9h）：封顶 30min 回看设置
        bj_10 = _utc(2026, 11, 3, 2, 0, 0)
        assert ClaimRoundManager._next_wait(1800, [19], bj_10) == 1800.0
        # 窗口近（北京 18:50）：准时醒来，不空转
        bj_1850 = _utc(2026, 11, 3, 10, 50, 0)
        assert ClaimRoundManager._next_wait(1800, [19], bj_1850) == pytest.approx(600.0)
        # 下限 30s（北京 18:59:50）：避免整点边界忙转
        bj_185950 = _utc(2026, 11, 3, 10, 59, 50)
        assert ClaimRoundManager._next_wait(1800, [19], bj_185950) == 30.0


class TestStoreParsing:
    def test_default_from_settings(self, fresh_app):
        assert fresh_app.claim_round_hours() == [19]  # settings.CLAIM_ROUND_HOURS 默认 "19"

    def test_full_day_forms(self, fresh_app):
        for raw in ("", "*"):
            fresh_app.set_setting("claim_round_hours", raw)
            assert fresh_app.claim_round_hours() == []

    def test_parse_and_normalize(self, fresh_app):
        fresh_app.set_setting("claim_round_hours", "21, 19，23")  # 半/全角逗号 + 空格
        assert fresh_app.claim_round_hours() == [19, 21, 23]

    def test_dirty_and_out_of_range_skipped(self, fresh_app):
        fresh_app.set_setting("claim_round_hours", "19, abc, 25, -1, 7")
        assert fresh_app.claim_round_hours() == [7, 19]

    def test_env_override_for_fresh_store(self, fresh_app, monkeypatch):
        monkeypatch.setattr(settings, "CLAIM_ROUND_HOURS", "8,22")
        assert Store().claim_round_hours() == [8, 22]
