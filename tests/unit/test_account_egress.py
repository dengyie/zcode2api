"""per-account 出口隔离（2.7.36）——账号绑定、tag 分键、gate 按出口隔离。

需求：每账号一个独立 IP 互相隔离（上游按出口 IP 风控；magpie 实证同 IP
3 账号即触发边缘 405）。开关关闭时全链路行为与共享出口时代逐字节一致。
"""

from __future__ import annotations

import pytest

from app import egress, settings
from app.gate import Gate
from app.models import Account
from app.store import Store


# ── egress.account_proxy / egress_tag_for ────────────────────────────────────
def test_account_proxy_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", False)
    acc = Account(id="a", name="a", provider="zai", mode="jwt", egress="http://127.0.0.1:11031")
    assert egress.account_proxy(acc) is None  # 开关关 = 行为零变化


def test_account_proxy_bound_and_unbound(monkeypatch):
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    bound = Account(id="a", name="a", provider="zai", mode="jwt",
                    egress="http://127.0.0.1:11031")
    unbound = Account(id="b", name="b", provider="zai", mode="jwt")
    assert egress.account_proxy(bound) == "http://127.0.0.1:11031"
    assert egress.account_proxy(unbound) is None  # 未绑定回落共享出口


def test_egress_tag_for_isolation_key(monkeypatch):
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    bound = Account(id="a", name="a", provider="zai", mode="jwt",
                    egress="http://127.0.0.1:11031")
    # per-account：tag = 绑定代理 URL（闸门/验证码池/config 缓存按它分键）
    assert egress.egress_tag_for(bound, "proxy") == "http://127.0.0.1:11031"
    # 未绑定：沿用调用方传统 2 值 tag（全局语义）
    assert egress.egress_tag_for(Account(id="b", name="b", provider="zai", mode="jwt"),
                                 "proxy") == "proxy"


# ── store 分配 ───────────────────────────────────────────────────────────────
def test_assign_egress_distinct_and_exhaustion(monkeypatch, fresh_app: Store):
    monkeypatch.setattr(settings, "EGRESS_LISTENERS",
                        ["http://127.0.0.1:11031", "http://127.0.0.1:11032"])
    a = fresh_app.add_account("zai", "a", "jwt.token.a")
    b = fresh_app.add_account("zai", "b", "jwt.token.b")
    c = fresh_app.add_account("zai", "c", "jwt.token.c")
    assert a.egress == "http://127.0.0.1:11031"
    assert b.egress == "http://127.0.0.1:11032"
    assert c.egress is None  # 池耗尽 → 回落共享出口，不崩溃
    # 已绑定的不受后续分配影响（稳定性 = 验证码 token 与 IP 绑定的前提）
    assert a.egress == "http://127.0.0.1:11031"


def test_ensure_egress_assignments_backfills(monkeypatch):
    monkeypatch.setattr(settings, "EGRESS_LISTENERS",
                        ["http://127.0.0.1:11031", "http://127.0.0.1:11032"])
    store = Store()  # 直接构造（conftest fresh_app 可能已带别的 listeners 状态）
    store._accounts = {p: [] for p in store._accounts}
    a = Account.create("zai", "a", "jwt.token.a")
    b = Account.create("zai", "b", "jwt.token.b")
    store._accounts["zai"] = [a, b]
    store.ensure_egress_assignments()
    assert a.egress and b.egress and a.egress != b.egress
    # 幂等：重跑不换绑定
    first = a.egress
    store.ensure_egress_assignments()
    assert a.egress == first


# ── gate 按出口分键 ──────────────────────────────────────────────────────────
def test_gate_edge_hold_is_per_tag(monkeypatch):
    monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 60)
    g = Gate()
    g.trip_edge("http://127.0.0.1:11031")
    assert g.refusal_reason("http://127.0.0.1:11031") == "edge_hold"
    assert g.acquire("http://127.0.0.1:11031") == (False, "edge_hold")
    # 其它出口不受牵连——per-account 隔离的核心价值
    assert g.refusal_reason("http://127.0.0.1:11032") is None
    assert g.acquire("http://127.0.0.1:11032") == (True, "")


def test_gate_busy_hold_is_per_tag(monkeypatch):
    monkeypatch.setattr(settings, "GATE_BUSY_THRESHOLD", 2)
    monkeypatch.setattr(settings, "GATE_BUSY_WINDOW_SECONDS", 60)
    monkeypatch.setattr(settings, "GATE_BUSY_HOLD_SECONDS", 60)
    g = Gate()
    g.note_busy("tag-a")
    g.note_busy("tag-a")
    assert g.refusal_reason("tag-a") == "busy_hold"
    assert g.refusal_reason("tag-b") is None


def test_gate_pool_hold_stays_global(monkeypatch):
    g = Gate()
    g.note_pool_exhausted(-1)
    # pool_hold 是全池额度证据语义，不分键
    assert g.refusal_reason("tag-a") == "pool_hold"
    assert g.refusal_reason("tag-b") == "pool_hold"


def test_gate_shared_tag_matches_legacy_behavior():
    """共享模式（所有账号同 tag）下 edge/busy 行为与旧全池语义一致。"""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 60)
    g = Gate()
    g.trip_edge("shared")
    assert g.acquire("shared") == (False, "edge_hold")
    monkey.undo()
