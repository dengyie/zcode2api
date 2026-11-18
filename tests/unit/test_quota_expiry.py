"""额度过期语义单元测试（2026-10-31 线上实证）。

背景：三个号的权益全部过期，但 `billing/balance` 仍报 `remaining_units=1e8`。
旧判定只看 `remaining <= 0`，于是「本地满额、上游每个请求都 1005」，账号长期
占着轮询位。修复后窗口有效性 = 剩余 > 0 **且** 未过期。
"""

from __future__ import annotations

import asyncio
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


class TestUnknownRemainingSemantics:
    """2.7.68：remaining 缺失/None = 未知但有量（与 window_usable 同口径），
    不得在 quota_verified_at 证据门之前短路判死。"""

    def test_unknown_remaining_without_verify_is_not_expired(self):
        acc = _acc({"GLM-5.3": {"total": 100, "used": 5,
                                "expires_at": _NOW - 60}})
        assert acc.quota_expired(_NOW) is False

    def test_unknown_remaining_with_post_expiry_verify_is_expired(self):
        acc = _acc({"GLM-5.3": {"total": 100, "used": 5,
                                "expires_at": _NOW - 60}},
                   quota_verified_at=_NOW - 1)
        assert acc.quota_expired(_NOW) is True

    def test_explicit_zero_remaining_is_expired_regardless(self):
        acc = _acc({"GLM-5.3": {"total": 100, "used": 100, "remaining": 0,
                                "expires_at": _NOW + 60}})
        assert acc.quota_expired(_NOW) is True

    def test_next_window_start_unknown_remaining_not_confirmed_dead(self):
        acc = _acc({"GLM-5.3": {"total": 100, "used": 5,
                                "expires_at": _NOW - 60}})
        # 未知余量 + 无过期后实查 → 不可判定（None），不得返回 -1/未来窗口
        assert acc.next_window_start(_NOW) is None

    def test_is_selectable_bonus_active_overrides_exhausted(self):
        """2.7.68 bonus-aware：日窗扣完但一次性赠送授权在效的号可选
        （quota.py 会把它复活成 ACTIVE，轮询必须真能选中它）。"""
        acc = _acc({"GLM-5.3": {"total": 100, "used": 100, "remaining": 0,
                                "expires_at": _NOW + 60}},
                   status=Status.EXHAUSTED)
        acc.plans = [{
            "plan_id": "zcode-v3-start-plan-1118",
            "ends_at": _NOW + 86400,
            "entitlements": [{"period": "one_time", "effective_at": _NOW - 10,
                              "ends_at": _NOW + 86400}],
        }]
        assert acc.is_selectable(_NOW) is True

    def test_is_selectable_expired_bonus_still_exhausted(self):
        acc = _acc({"GLM-5.3": {"total": 100, "used": 100, "remaining": 0,
                                "expires_at": _NOW + 60}},
                   status=Status.EXHAUSTED)
        acc.plans = [{
            "plan_id": "zcode-v3-start-plan-1116",
            "ends_at": _NOW - 10,  # 套餐已到期 → 赠送随之失效
            "entitlements": [{"period": "one_time", "effective_at": _NOW - 20,
                              "ends_at": _NOW - 10}],
        }]
        assert acc.is_selectable(_NOW) is False


class TestRefreshCoalescing:
    """2.7.68 跨入口刷新合并：同账号并发 refresh_accounts 只跑一次真实刷新。"""

    @pytest.mark.asyncio
    async def test_concurrent_refresh_coalesces_same_account(self, monkeypatch):
        import app.quota as quota_module
        from app.models import Account

        acc = Account(id="coal-1", name="coal", provider="zai", mode="jwt",
                      jwt_token="a.b.c")
        calls = {"n": 0, "max": 0, "inflight": 0}

        async def fake_fetch(acc):
            calls["inflight"] += 1
            calls["max"] = max(calls["max"], calls["inflight"])
            await asyncio.sleep(0.05)
            calls["inflight"] -= 1
            calls["n"] += 1
            return {"billing": {}}

        monkeypatch.setattr(quota_module, "fetch_quota", fake_fetch)
        await asyncio.gather(quota_module.refresh_accounts([acc]),
                             quota_module.refresh_accounts([acc]),
                             quota_module.refresh_accounts([acc]))
        assert calls["n"] == 1, "同账号 3 路并发必须合并为 1 次真实刷新"
        assert calls["max"] == 1
