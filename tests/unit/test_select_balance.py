"""store.select 负载均衡选号：同负载平局下剩余额度优先（2.7.46）。

主排序 inflight 不变；次排序 -max(remaining)（额度快照未知记 0，全池未知
退化为纯轮询）；末排序轮询偏移（额度同档均匀轮换）。
背景（2026-11-17 线上实证）：凌晨全池 inflight=0 全平局时纯轮询把 5M 小窗
与 3.05 亿大窗同权重随机分，use_count 长期失衡（4034 vs 68）。
"""
import time

import pytest

from app.models import Account, Status
from app.store import Store


def _acc(store: Store, name: str, remaining: int | None, model: str = "GLM-5.3-Flash") -> Account:
    quota = {} if remaining is None else {model: {"total": remaining, "used": 0, "remaining": remaining}}
    a = Account.create("zai", name, f"tok-{name}")
    a.quota = quota
    a.quota_verified_at = time.time()
    store.add_account("zai", name, f"tok-{name}")
    # add_account 返回的是 store 内对象；写 quota 到 live 对象
    live = next(x for x in store.list_accounts("zai") if x.name == name)
    live.quota = quota
    return live


@pytest.fixture
def store(fresh_app: Store) -> Store:
    return fresh_app


class TestBalanceAwareSelection:
    def test_richer_account_wins_tie(self, store: Store):
        """同 inflight=0 平局：3.05 亿大窗优先于 5M 小窗。"""
        _acc(store, "small", remaining=5_000_000)
        _acc(store, "rich", remaining=305_000_000)
        got = store.select("zai", inflight={}, max_inflight=3)
        assert got.name == "rich"

    def test_inflight_still_dominates(self, store: Store):
        """主排序不变：小余额但空载 > 大余额但满载。"""
        live_rich = _acc(store, "rich-busy", remaining=305_000_000)
        _acc(store, "small-idle", remaining=5_000_000)
        got = store.select(
            "zai",
            inflight={live_rich.id: 3},  # 达上限（按 id 过滤）
            max_inflight=3,
        )
        # rich-busy 被并发上限过滤；small-idle 是唯一候选
        assert got.name == "small-idle"

    def test_inflight_low_wins_over_quota(self, store: Store):
        """inflight=1 的小余额号 优先于 inflight=0 的大余额号？否——inflight 主排序：
        空载大余额 > 1 路小余额。这里验证小余额空载 vs 大余额 1 路：选小余额。"""
        _acc(store, "small-idle", remaining=5_000_000)
        live_rich = _acc(store, "rich-1w", remaining=305_000_000)
        got = store.select("zai", inflight={live_rich.id: 1}, max_inflight=3)
        assert got.name == "small-idle"

    def test_unknown_quota_degrades_to_round_robin(self, store: Store):
        """额度快照未知（新号）记 0：两个未知号之间仍轮换，不总是第一个。"""
        _acc(store, "unk-a", remaining=None)
        _acc(store, "unk-b", remaining=None)
        first = store.select("zai", inflight={}, max_inflight=3).name
        second = store.select("zai", inflight={}, max_inflight=3).name
        assert {first, second} == {"unk-a", "unk-b"}

    def test_unknown_prefers_known_when_tied(self, store: Store):
        """未知快照（记 0）在同负载下劣于已知有量的号。"""
        _acc(store, "new-unk", remaining=None)
        _acc(store, "known", remaining=1000)
        assert store.select("zai", inflight={}, max_inflight=3).name == "known"

    def test_equal_quota_rotates(self, store: Store):
        """额度同档：轮询偏移仍生效（不总选同一个）。"""
        _acc(store, "eq-a", remaining=305_000_000)
        _acc(store, "eq-b", remaining=305_000_000)
        picks = {store.select("zai", inflight={}, max_inflight=3).name for _ in range(2)}
        assert picks == {"eq-a", "eq-b"}

    def test_max_across_windows(self, store: Store):
        """多模型窗口取 max（窗口互不抵扣）：小 Flash 窗 + 大 Coding 窗 = 大。"""
        _acc(store, "multi", remaining=5_000_000)
        live = next(x for x in store.list_accounts("zai") if x.name == "multi")
        live.quota = {
            "GLM-5.3-Flash": {"total": 5_000_000, "remaining": 5_000_000},
            "GLM-5.3-Coding": {"total": 300_000_000, "remaining": 300_000_000},
        }
        _acc(store, "flashonly", remaining=100_000_000)
        got = store.select("zai", inflight={}, max_inflight=3)
        assert got.name == "multi"

    def test_pinned_still_first(self, store: Store):
        """亲和优先级高于余额排序。"""
        rich = _acc(store, "rich", remaining=305_000_000)
        _acc(store, "small", remaining=5_000_000)
        got = store.select(
            "zai", inflight={}, pinned_id=rich.id, max_inflight=3,
        )
        assert got.name == "rich"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class TestWindowUsableAlignment:
    """2.7.51 口径对齐：_max_remaining 只数 window_usable 的窗口。"""

    def test_expired_big_window_not_rich(self, store: Store):
        """过期大窗不排富：A{Flash 3亿 已过期 + Coding 5M 可用} 输给
        B{Flash 1亿 可用}。线上真实形态：Flash 日窗 23:59 过期 + 快照陈旧期。"""
        live_a = _acc(store, "expired-rich", remaining=5_000_000)
        live = next(x for x in store.list_accounts("zai") if x.name == "expired-rich")
        live.quota = {
            "GLM-5.3-Flash": {
                "total": 305_000_000, "remaining": 305_000_000,
                "expires_at": _iso(time.time() - 3600),
            },
            "GLM-5.3-Coding": {"total": 5_000_000, "remaining": 5_000_000},
        }
        _acc(store, "healthy", remaining=100_000_000)
        got = store.select("zai", inflight={}, max_inflight=3)
        assert got.name == "healthy"
        # A 自身仍可选（另窗可用），只是不凭过期大窗排富
        assert live_a.status == Status.ACTIVE

    def test_missing_remaining_backfills_total_used(self, store: Store):
        """窗口缺 remaining 按 total-used 回填（window_usable「未知但有量」同源）。"""
        live = _acc(store, "implicit", remaining=None)
        live = next(x for x in store.list_accounts("zai") if x.name == "implicit")
        live.quota = {
            "GLM-5.3": {"total": 100_000_000, "used": 30_000_000},  # 无 remaining
        }
        _acc(store, "explicit-small", remaining=50_000_000)
        # 隐含 70M > 显式 50M → 隐含号优先
        got = store.select("zai", inflight={}, max_inflight=3)
        assert got.name == "implicit"

    def test_zero_remaining_after_backfill_counts_zero(self, store: Store):
        """total-used 回填为 0 的窗口不排富（用尽但未过期），退化为轮询偏移。"""
        live = _acc(store, "drained", remaining=None)
        live = next(x for x in store.list_accounts("zai") if x.name == "drained")
        live.quota = {"GLM-5.3": {"total": 10_000_000, "used": 10_000_000}}
        _acc(store, "unknown", remaining=None)
        got = store.select("zai", inflight={}, max_inflight=3)
        # drained 有效余量 0 == unknown 记 0 → 轮询偏移平局，两者都合法
        assert got.name in ("drained", "unknown")

    def test_all_expired_degrades_to_zero(self, store: Store):
        """全部窗口过期 = 记 0，与未知新号同档（轮询偏移平局），不排富。"""
        live = _acc(store, "all-expired", remaining=None)
        live = next(x for x in store.list_accounts("zai") if x.name == "all-expired")
        live.quota = {
            "GLM-5.3-Flash": {
                "total": 300_000_000, "remaining": 300_000_000,
                "expires_at": _iso(time.time() - 7200),
            },
        }
        _acc(store, "new-unk", remaining=None)
        got = store.select("zai", inflight={}, max_inflight=3)
        assert got.name in ("all-expired", "new-unk")
