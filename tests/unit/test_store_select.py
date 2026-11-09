"""Store.select 负载均衡与亲和性选择单元测试。"""

from __future__ import annotations

import time

from app.models import Status
from app.store import Store


class TestStoreSelectLoadBalancing:
    def test_least_inflight_preferred(self, fresh_app: Store):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        b = fresh_app.add_account("zai", "b", "jwt.token.b")

        # a 有 1 个在飞请求，b 有 0 个在飞请求 -> 优先选负载最小的 b
        inflight = {a.id: 1, b.id: 0}
        pick = fresh_app.select("zai", inflight=inflight)
        assert pick is not None
        assert pick.id == b.id

    def test_round_robin_when_equal_inflight(self, fresh_app: Store):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        b = fresh_app.add_account("zai", "b", "jwt.token.b")

        # 负载相同（均为 0），依次轮流分流
        inflight = {a.id: 0, b.id: 0}
        picks = [fresh_app.select("zai", inflight=inflight).id for _ in range(4)]
        assert picks == [a.id, b.id, a.id, b.id]

    def test_pinned_id_honored_when_has_capacity(self, fresh_app: Store):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        b = fresh_app.add_account("zai", "b", "jwt.token.b")

        # 尽管 a 有 1 个在飞请求、b 有 0 个，但 a 是会话亲和号且未达上限 2 -> 坚持选 a
        inflight = {a.id: 1, b.id: 0}
        pick = fresh_app.select("zai", inflight=inflight, pinned_id=a.id)
        assert pick is not None
        assert pick.id == a.id

    def test_pinned_id_saturated_spillover(self, fresh_app: Store):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        b = fresh_app.add_account("zai", "b", "jwt.token.b")

        # a 是亲和号，但已达并发上限 2（2/2） -> 自动溢出到有余量的 b（0/2）
        inflight = {a.id: 2, b.id: 0}
        pick = fresh_app.select("zai", inflight=inflight, pinned_id=a.id)
        assert pick is not None
        assert pick.id == b.id

    def test_pinned_id_disabled_or_cooling_spillover(self, fresh_app: Store):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        b = fresh_app.add_account("zai", "b", "jwt.token.b")

        # a 亲和号被风控禁用
        fresh_app.set_enabled("zai", a.id, False)
        pick = fresh_app.select("zai", pinned_id=a.id)
        assert pick is not None
        assert pick.id == b.id

        # a 恢复但进入冷却期
        fresh_app.set_enabled("zai", a.id, True)
        a.status = Status.COOLING
        a.cooling_until = time.time() + 300
        fresh_app.update_account(a)

        pick2 = fresh_app.select("zai", pinned_id=a.id)
        assert pick2 is not None
        assert pick2.id == b.id

    def test_all_saturated_returns_none(self, fresh_app: Store):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        b = fresh_app.add_account("zai", "b", "jwt.token.b")

        # 全满（均达 2 并发） -> 返回 None（触发网关 503）
        inflight = {a.id: 2, b.id: 2}
        pick = fresh_app.select("zai", inflight=inflight)
        assert pick is None
