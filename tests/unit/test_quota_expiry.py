"""额度过期语义单元测试（2026-10-31 线上实证）。

背景：三个号的权益全部过期，但 `billing/balance` 仍报 `remaining_units=1e8`。
旧判定只看 `remaining <= 0`，于是「本地满额、上游每个请求都 1005」，账号长期
占着轮询位。修复后窗口有效性 = 剩余 > 0 **且** 未过期。
"""

from __future__ import annotations

import time

import pytest

from app.models import Account, Status, window_expired, window_usable

_NOW = 1_000_000.0


def _acc(quota=None, **kw) -> Account:
    return Account(
        id="acc-1", name="acc", provider="zai", mode="jwt",
        jwt_token="a.b.c", quota=quota or {}, **kw,
    )


class TestWindowHelpers:
    def test_usable_when_amount_and_not_expired(self):
        assert window_usable({"remaining": 10, "expires_at": _NOW + 60}, _NOW) is True

    def test_expired_even_with_amount(self):
        # 线上真实形态：剩余 1e8，但窗口 28 天前就过期
        w = {"remaining": 100_000_000, "expires_at": _NOW - 60}
        assert window_usable(w, _NOW) is False
        assert window_expired(w, _NOW) is True

    def test_zero_remaining_not_expired(self):
        w = {"remaining": 0, "expires_at": _NOW + 60}
        assert window_usable(w, _NOW) is False
        assert window_expired(w, _NOW) is False

    def test_missing_expires_at_means_open_ended(self):
        assert window_usable({"remaining": 1}, _NOW) is True
        assert window_expired({"remaining": 1}, _NOW) is False
        assert window_usable({"remaining": 1, "expires_at": None}, _NOW) is True

    def test_missing_remaining_is_unknown_not_zero(self):
        assert window_usable({"total": 100, "used": 10}, _NOW) is True

    def test_boundary_expired_at_now(self):
        assert window_usable({"remaining": 1, "expires_at": _NOW}, _NOW) is False

    @pytest.mark.parametrize("bad", [None, {}, "x", 0])
    def test_malformed_window(self, bad):
        assert window_usable(bad, _NOW) is False
        assert window_expired(bad, _NOW) is False


class TestQuotaExpired:
    def test_empty_quota_is_unknown_not_expired(self):
        """空快照 = 还没查过，不能当成过期（否则新号永远选不上）。"""
        assert _acc().quota_expired(_NOW) is False

    def test_all_windows_expired(self):
        acc = _acc({
            "GLM-5.3": {"remaining": 0, "expires_at": _NOW - 10},
            "GLM-5.3-Flash": {"remaining": 100_000_000, "expires_at": _NOW - 86400},
        })
        assert acc.quota_expired(_NOW) is True

    def test_one_live_window_is_enough(self):
        acc = _acc({
            "GLM-5.3": {"remaining": 0, "expires_at": _NOW + 60},
            "GLM-5.3-Flash": {"remaining": 5, "expires_at": _NOW + 60},
        })
        assert acc.quota_expired(_NOW) is False

    def test_all_zero_remaining_is_expired_too(self):
        acc = _acc({"GLM-5.3": {"remaining": 0, "expires_at": _NOW + 60}})
        assert acc.quota_expired(_NOW) is True


class TestSelectable:
    def test_expired_quota_not_selectable(self):
        acc = _acc({"GLM-5.3-Flash": {"remaining": 100_000_000, "expires_at": _NOW - 86400}})
        assert acc.is_selectable(_NOW) is False

    def test_live_quota_selectable(self):
        acc = _acc({"GLM-5.3-Flash": {"remaining": 10, "expires_at": _NOW + 60}})
        assert acc.is_selectable(_NOW) is True

    def test_empty_quota_selectable(self):
        assert _acc().is_selectable(_NOW) is True

    def test_fallback_account_ignores_plan_quota_expiry(self):
        """JWT 死了但有 API Key 的号走 api.z.ai，花 Key 自己的额度，不该被套餐快照挡住。"""
        acc = _acc(
            {"GLM-5.3": {"remaining": 0, "expires_at": _NOW - 10}},
            status=Status.INVALID, api_key="sk-test",
        )
        assert acc.is_selectable(_NOW) is True

    def test_expired_quota_defaults_to_real_now(self):
        """不传 now 时用真实时钟（回归：曾把 expires_at 当秒数比较而漏判）。"""
        acc = _acc({"GLM-5.3": {"remaining": 10, "expires_at": time.time() - 10}})
        assert acc.is_selectable() is False
