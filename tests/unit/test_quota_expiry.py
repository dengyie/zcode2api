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
        """2026-10-31 线上形态：快照是过期**之后**取的 → 已确认，判死。"""
        acc = _acc({
            "GLM-5.3": {"remaining": 0, "expires_at": _NOW - 10},
            "GLM-5.3-Flash": {"remaining": 100_000_000, "expires_at": _NOW - 86400},
        }, quota_verified_at=_NOW - 5)
        assert acc.quota_expired(_NOW) is True

    def test_expired_snapshot_without_post_expiry_check_is_unknown(self):
        """2026-11-03 线上形态：日窗滚动，快照停在上一轮 → 不能据此判死。

        实证：trial 号 `expires_at=11-02 23:59:59`，11-03 00:39 上游照回 200
        （新窗到 11-03 23:59:59）。快照取自过期之前，等于没有「过期后」的证据。
        """
        acc = _acc({"GLM-5.3-Flash": {"remaining": 100_000_000, "expires_at": _NOW - 600}},
                   quota_verified_at=_NOW - 3600)  # 查询发生在到期之前
        assert acc.quota_expired(_NOW) is False

    def test_never_refreshed_expired_snapshot_is_unknown(self):
        acc = _acc({"GLM-5.3-Flash": {"remaining": 100, "expires_at": _NOW - 600}})
        assert acc.quota_expired(_NOW) is False

    def test_one_live_window_is_enough(self):
        acc = _acc({
            "GLM-5.3": {"remaining": 0, "expires_at": _NOW + 60},
            "GLM-5.3-Flash": {"remaining": 5, "expires_at": _NOW + 60},
        })
        assert acc.quota_expired(_NOW) is False

    def test_all_zero_remaining_is_expired_too(self):
        """余额归零是确定耗尽，与快照新旧无关（不需要过期后确认）。"""
        acc = _acc({"GLM-5.3": {"remaining": 0, "expires_at": _NOW + 60}})
        assert acc.quota_expired(_NOW) is True

    def test_iso_expiry_parsed(self):
        import time as _t

        iso = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_NOW - 60))
        acc = _acc({"GLM-5.3": {"remaining": 10, "expires_at": iso}}, quota_verified_at=_NOW)
        assert acc.quota_expired(_NOW) is True


class TestSelectable:
    def test_expired_quota_not_selectable(self):
        acc = _acc({"GLM-5.3-Flash": {"remaining": 100_000_000, "expires_at": _NOW - 86400}},
                   quota_verified_at=_NOW - 60)
        assert acc.is_selectable(_NOW) is False

    def test_stale_expired_quota_still_selectable(self):
        """快照过时（查询早于到期）不算判死——让请求去问上游，而不是 503。"""
        acc = _acc({"GLM-5.3-Flash": {"remaining": 100, "expires_at": _NOW - 60}})
        assert acc.is_selectable(_NOW) is True

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
        """不传 now 时用真实时钟（回归：曾把 expires_at 当秒数比较而漏判）。

        需要「过期后成功查过」才判死（滚动日窗，见 quota_expired）。
        """
        now = time.time()
        acc = _acc({"GLM-5.3": {"remaining": 10, "expires_at": now - 10}},
                   quota_verified_at=now)
        assert acc.is_selectable() is False
