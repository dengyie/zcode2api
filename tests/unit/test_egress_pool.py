"""TetherPool 出口管理器（2.7.39 Mode B）——URL 构造 / rebalance 编排 / 互斥。

核心不变式：
- 网关 URL 确定性构造（rebalance 不改 URL，只改 TP 服务端绑定）
- 账号间节点互斥（exclude_node = 其它账号的 egress_node，风控核心）
- rebalance 探针验收（JSON-APP 放行 / HTML-EDGE 换下一个）
- 探针/HTTP 异常安全（PROBE-FAIL ≠ 边缘封禁，前者换节点后者隔离）
"""

from __future__ import annotations

import asyncio

import pytest

from app import egress, egress_pool, settings
from app.egress_pool import gateway_url_for
from app.models import Account


@pytest.fixture
def tp_mode(monkeypatch):
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "tetherpool")
    monkeypatch.setattr(settings, "TP_GATEWAY", "127.0.0.1:9101")
    monkeypatch.setattr(settings, "TP_TYPE", "clash_proxy")
    monkeypatch.setattr(settings, "TP_REGION", "us-west")
    monkeypatch.setattr(settings, "TP_TIER", "standard")
    monkeypatch.setattr(settings, "TP_SERVICE", "zcode-hub")
    monkeypatch.setattr(settings, "TP_REBALANCE_DEBOUNCE", 0)


def _account(acc_id="acc-xyz", name="a1", jwt=None):
    return Account(id=acc_id, name=name, provider="zai", mode="jwt",
                   jwt_token=jwt or "h.eyJzdWIiOiJ1In0.sig", egress_node=None)


def test_gateway_url_matches_tp_live_format(tp_mode):
    url = gateway_url_for(_account(acc_id="acc-xyz"))
    # 与 TP 线上 acquire 返回的 gateway_proxy_url 同构（username 承载约束）
    assert url.startswith("http://zh-acc-xyz__type_clash_proxy__region_us-west"
                          "__tier_standard__service_zcode-hub:tetherpool@")
    assert ":9101" in url


def test_gateway_url_is_stable_across_calls(tp_mode):
    acc = _account()
    assert gateway_url_for(acc) == gateway_url_for(acc)


def test_disabled_supply_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", False)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "tetherpool")
    acc = _account()
    assert egress.account_proxy(acc) is None  # 开关关 = 零行为


# ── rebalance 编排（stub TP HTTP 与探针）─────────────────────────────────────
class _StubPool:
    """替换 egress_pool 模块内的 TP HTTP 与探针，记录调用序列。"""

    def __init__(self, acquire_responses, probe_results):
        self.acquire_responses = list(acquire_responses)  # dict | ("fail", st)
        self.probe_results = list(probe_results)          # (st, raw)
        self.bans: list[str] = []
        self.acquired: list[dict] = []

    async def acquire(self, account, force_rebalance=False, extra_exclude=None):
        self.acquired.append({"force": force_rebalance,
                              "exclude": list(extra_exclude or [])})
        item = self.acquire_responses.pop(0)
        if isinstance(item, tuple):
            return None
        return item

    async def ban(self, node, reason, evidence, account_name):
        self.bans.append(node)


@pytest.mark.asyncio
async def test_rebalance_happy_path_updates_node(tp_mode, monkeypatch):
    acc = _account()
    acc.egress_node = "clash-301-pigeonw-01"
    stub = _StubPool(
        [{"node_name": "clash-302-pigeonw-02", "exit_ip": "23.230.200.36"}],
        [(405, '{"code":3012}')],  # JSON-APP = 边缘放行（3012 形状预期值）
    )
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)

    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=True)

    assert ok is True
    assert acc.egress_node == "clash-302-pigeonw-02"
    assert stub.bans == ["clash-301-pigeonw-01"]  # 旧节点被隔离
    assert stub.acquired[0]["force"] is True


@pytest.mark.asyncio
async def test_rebalance_bans_dead_probes_then_succeeds(tp_mode, monkeypatch):
    """探针 HTML-EDGE（仍被封）→ ban 该节点 → force_rebalance 换下一个。"""
    acc = _account(acc_id="acc-y")
    acc.egress_node = "old-node"
    stub = _StubPool(
        [{"node_name": "cand-1", "exit_ip": "1.2.3.4"},
         {"node_name": "cand-2", "exit_ip": "5.6.7.8"}],
        [(405, "<!doctype html>405"), (405, '{"code":3012}')],
    )
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)
    probe_rounds = {"n": 0}

    async def _probe_seq(self, account, proxy_url, jwt):
        probe_rounds["n"] += 1
        # 第 1 针仍边缘封禁（换下一个候选），第 2 针放行
        return (405, "<!doctype html>405") if probe_rounds["n"] == 1 \
            else (405, '{"code":3012}')

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_seq)

    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=True)

    assert ok is True
    assert acc.egress_node == "cand-2"
    assert stub.bans == ["old-node", "cand-1"]  # 死探针的候选也被隔离


@pytest.mark.asyncio
async def test_rebalance_pool_exhausted_keeps_binding(tp_mode, monkeypatch):
    acc = _account(acc_id="acc-z")
    acc.egress_node = "old-node"
    stub = _StubPool([("fail", 503)], [])
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_200(self, account, proxy_url, jwt):
        return 200, "{}"

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_200)

    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=True)

    assert ok is False
    assert acc.egress_node == "old-node"  # 池枯保持旧绑定（绝不本地降级）


@pytest.mark.asyncio
async def test_rebalance_exclude_node_enforces_isolation(tp_mode, monkeypatch):
    """互斥（风控核心）：acquire 的 exclude_node 必须携带其它账号的节点。"""
    seen_exclude = []

    async def _spy_acquire(account, force_rebalance=False, extra_exclude=None):
        seen_exclude.append(list(extra_exclude or []))
        return {"node_name": "cand-9", "exit_ip": "9.9.9.9"}

    acc = _account(acc_id="acc-x")
    monkeypatch.setattr(egress_pool, "tp_acquire", _spy_acquire)
    monkeypatch.setattr(egress_pool, "_other_nodes",
                        lambda account: ["clash-303-pigeonw-03"])
    monkeypatch.setattr(egress_pool, "tp_ban", lambda *a, **k: None)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)

    pool = egress_pool.TetherPoolEgress()
    await pool.rebalance(acc, reason="edge_405_html", ban=False)

    assert seen_exclude and seen_exclude[0] == ["clash-303-pigeonw-03"]


def test_conn_fail_clustering_window():
    """连接失败聚类（节点不可达触发器）：窗口内 ≥3 次 = clustered。"""
    import time as _t

    tag = "http://gw:9101"
    egress.clear_conn_fails(tag)
    base = _t.monotonic()
    for i in range(2):
        egress.note_conn_fail(tag, now=base + i)
    assert not egress.conn_fail_clustered(tag)
    egress.note_conn_fail(tag, now=base + 2)
    assert egress.conn_fail_clustered(tag)
    # 窗口外不算
    egress.clear_conn_fails(tag)
    egress.note_conn_fail(tag, now=base - 700)
    assert not egress.conn_fail_clustered(tag)
    egress.clear_conn_fails(tag)


def test_conn_fail_tags_are_isolated():
    tag_a, tag_b = "http://gw-a:9101", "http://gw-b:9101"
    egress.clear_conn_fails(tag_a)
    egress.clear_conn_fails(tag_b)
    for _ in range(3):
        egress.note_conn_fail(tag_a)
    assert egress.conn_fail_clustered(tag_a)
    assert not egress.conn_fail_clustered(tag_b)  # per-account 隔离
    egress.clear_conn_fails(tag_a)
    egress.clear_conn_fails(tag_b)


@pytest.mark.asyncio
async def test_health_loop_bootstraps_binding_and_probes(tp_mode, monkeypatch):
    """健康循环：未绑定账号先 ensure_binding（egress_node 落地 = 风控互斥前提），
    已绑定账号探针失败 → rebalance(ban=False)。"""
    from app import store as store_mod

    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)
    monkeypatch.setattr(settings, "TP_HEALTH_INTERVAL", 0)

    acc = store.add_account("zai", "h1", "h.eyJzdWIiOiJ1In0.sig")
    acc.id = "acc-h1"
    acc.enabled = True

    calls = {"bind": 0, "probe": 0, "rebalance": 0}

    async def fake_tp_acquire(account, force_rebalance=False, extra_exclude=None):
        calls["bind"] += 1
        account.egress_node = "node-boot"
        return {"node_name": "node-boot"}

    async def fake_health_once(account):
        calls["probe"] += 1
        return False

    async def fake_rebalance(account, reason, ban=True):
        calls["rebalance"] += 1

    monkeypatch.setattr(egress_pool, "tp_acquire", fake_tp_acquire)
    monkeypatch.setattr(egress_pool.egress_pool, "health_once", fake_health_once)
    monkeypatch.setattr(egress_pool.egress_pool, "rebalance", fake_rebalance)

    egress_pool.egress_pool.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await egress_pool.egress_pool.stop()
    assert calls["bind"] >= 1
    assert acc.egress_node == "node-boot"
    assert calls["probe"] >= 1
    assert calls["rebalance"] >= 1


@pytest.mark.asyncio
async def test_verify_then_rebalance_transient_keeps_binding(tp_mode, monkeypatch):
    """连接失败触发器：探针通 = 瞬态抖动 → 保持绑定（防节点漂移）。"""
    rebalanced = []

    async def fake_health(account):
        return True  # 节点活着

    async def fake_rebalance(account, reason, ban=True):
        rebalanced.append(reason)

    monkeypatch.setattr(egress_pool.egress_pool, "health_once", fake_health)
    monkeypatch.setattr(egress_pool.egress_pool, "rebalance", fake_rebalance)

    acc = _account(acc_id="acc-v1", name="v1")
    acc.egress_node = "node-alive"
    await egress_pool.egress_pool.verify_then_rebalance(acc, reason="node_unreachable")
    assert rebalanced == []  # 瞬态不换节点
    assert acc.egress_node == "node-alive"


@pytest.mark.asyncio
async def test_verify_then_rebalance_dead_node_swaps(tp_mode, monkeypatch):
    """连接失败触发器：探针不通 = 节点死 → rebalance 换绑。"""
    rebalanced = []

    async def fake_health(account):
        return False  # 节点死了

    async def fake_rebalance(account, reason, ban=True):
        rebalanced.append((reason, ban))

    monkeypatch.setattr(egress_pool.egress_pool, "health_once", fake_health)
    monkeypatch.setattr(egress_pool.egress_pool, "rebalance", fake_rebalance)

    acc = _account(acc_id="acc-v2", name="v2")
    acc.egress_node = "node-dead"
    await egress_pool.egress_pool.verify_then_rebalance(acc, reason="node_unreachable")
    assert rebalanced == [("node_unreachable", False)]  # 连接失败不 ban 只换


@pytest.mark.asyncio
async def test_verify_then_rebalance_unbound_bootstraps(tp_mode, monkeypatch):
    """连接失败触发器：未绑定账号直接走 rebalance（内部含 acquire+探针）。"""
    rebalanced = []

    async def fake_rebalance(account, reason, ban=True):
        rebalanced.append(reason)

    monkeypatch.setattr(egress_pool.egress_pool, "rebalance", fake_rebalance)

    acc = _account(acc_id="acc-v3", name="v3")
    assert acc.egress_node is None
    await egress_pool.egress_pool.verify_then_rebalance(acc, reason="node_unreachable")
    assert rebalanced == ["node_unreachable"]
