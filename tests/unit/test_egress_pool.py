"""TetherPool 出口管理器（2.7.39 Mode B）——URL 构造 / rebalance 编排 / 互斥。

核心不变式：
- 网关 URL 确定性构造（rebalance 不改 URL，只改 TP 服务端绑定）
- 账号间节点互斥（exclude_node = 其它账号的 egress_node，风控核心）
- rebalance 探针验收（JSON-APP 放行 / HTML-EDGE 换下一个）
- 探针/HTTP 异常安全（PROBE-FAIL ≠ 边缘封禁，前者换节点后者隔离）
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from app import egress, egress_pool, settings
from app import store as store_mod
from app.egress_pool import gateway_url_for
from app.models import Account, Status

# autouse fixture 会把 probe_alicdn 换成恒过假探针；probe_alicdn 本体的
# 单测需要真实现——在 import 期（fixture 生效前）捕获原函数引用。
_REAL_PROBE_ALICDN = egress_pool.probe_alicdn
_REAL_TP_SNAPSHOT = egress_pool._tp_live_snapshot


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


@pytest.fixture(autouse=True)
def _alicdn_probe_ok(monkeypatch):
    """默认 alicdn 探针恒过 + TP 实时对账快照为空（两者都是网络调用，单测
    不得出网）。需要失败/冲突语义的测试在用例内自行 monkeypatch 覆盖
    （同属性后写胜出）。"""
    async def _ok(proxy_url, *, full=False, fetch_impl=None):
        return True, 0.1, "ok"
    monkeypatch.setattr(egress_pool, "probe_alicdn", _ok)

    async def _empty_snapshot():
        return {}, set()
    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _empty_snapshot)


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
    # 2.7.68 ban 延后到确认离开：轮内死探针候选照常隔离，orig_node 在成功
    # 换绑后才隔离（旧实现 attempt-1 先 ban old-node，TP 粘性回同节点时
    # 唯一可用节点进隔离台账且降级接受路径被堵死）
    assert stub.bans == ["cand-1", "old-node"]


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
async def test_health_loop_bootstraps_binding_and_probes(tp_mode, monkeypatch, tmp_path):
    """健康循环：未绑定账号先 ensure_binding（egress_node 落地 = 风控互斥前提），
    已绑定账号探针失败 → rebalance(ban=False)。DB 走 tmp_path 隔离
    （Store() 直开真实 dev 库已两次污染：2026-11-14 ×8 行 / 2026-11-15 ×2 行）。"""

    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test-accounts.db")
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)
    monkeypatch.setattr(settings, "TP_HEALTH_INTERVAL", 0)

    acc = store.add_account("zai", "h1", "h.eyJzdWIiOiJ1In0.sig")
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
async def test_health_probe_single_fail_does_not_rebalance(tp_mode, monkeypatch, tmp_path):
    """探针失败防抖（2026-11-15 生产数据：链路秒级抖动连坐 4 账号齐换绑）：
    单轮失败只留痕，连续 TP_HEALTH_FAIL_THRESHOLD 轮失败才 rebalance；
    中途探针转好 → streak 清零不再累计。"""

    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test-accounts.db")
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)
    monkeypatch.setattr(settings, "TP_HEALTH_FAIL_THRESHOLD", 3)

    acc = store.add_account("zai", "h1", "h.eyJzdWIiOiJ1In0.sig")
    acc.enabled = True
    acc.egress_node = "node-a"  # 已绑定，跳过引导分支

    calls = {"rebalance": 0, "probe": 0}

    async def fake_health_once(account):
        calls["probe"] += 1
        # 失败×2 → 成功×1（清零）→ 失败×3（第 3 轮才 rebalance）
        return calls["probe"] == 3

    async def fake_rebalance(account, reason, ban=True):
        calls["rebalance"] += 1

    monkeypatch.setattr(egress_pool.egress_pool, "health_once", fake_health_once)
    monkeypatch.setattr(egress_pool.egress_pool, "rebalance", fake_rebalance)

    for _ in range(6):
        await egress_pool.egress_pool._check_account(acc)
    # 轮次 1-2 失败不换；轮 3 成功清零；轮 4-5 失败不换；轮 6 第 3 连败才换
    assert calls["rebalance"] == 1
    assert egress_pool.egress_pool._probe_fail_streak.get(acc.id, 0) == 0


@pytest.mark.asyncio
async def test_verify_then_rebalance_transient_keeps_binding(tp_mode, monkeypatch):
    """连接失败触发器：探针通 = 瞬态抖动 → 保持绑定（防节点漂移）。"""
    rebalanced = []

    async def fake_health(account):
        return True  # 节点活着

    async def fake_rebalance(account, reason, ban=True):
        rebalanced.append(reason)

    monkeypatch.setattr(egress_pool.egress_pool, "health_once", fake_health)
    monkeypatch.setattr(egress_pool.egress_pool, "_rebalance_locked", fake_rebalance)

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
    monkeypatch.setattr(egress_pool.egress_pool, "_rebalance_locked", fake_rebalance)

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

    monkeypatch.setattr(egress_pool.egress_pool, "_rebalance_locked", fake_rebalance)

    acc = _account(acc_id="acc-v3", name="v3")
    assert acc.egress_node is None
    await egress_pool.egress_pool.verify_then_rebalance(acc, reason="node_unreachable")
    assert rebalanced == ["node_unreachable"]


@pytest.mark.asyncio
async def test_ensure_binding_passes_exclude_node(tp_mode, monkeypatch):
    """绑定引导（风控核心）：首次 acquire 必须带 exclude_node=其它账号节点。"""

    monkeypatch.setattr(settings, "DB_PATH", tmp_db_factory(monkeypatch))
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)  # _other_nodes 读模块级单例

    captured = {}

    async def fake_tp_acquire(account, force_rebalance=False, extra_exclude=None):
        captured["exclude"] = extra_exclude
        account.egress_node = "node-new"
        return {"node_name": "node-new"}

    monkeypatch.setattr(egress_pool, "tp_acquire", fake_tp_acquire)

    other = store.add_account("zai", "other", "h.eyJzdWIiOiJvIn0.sig")
    other.egress_node = "node-held-by-other"
    me = store.add_account("zai", "me", "h.eyJzdWIiOiJtIn0.sig")

    ok = await egress_pool.egress_pool.ensure_binding(me)
    assert ok
    assert "node-held-by-other" in (captured["exclude"] or []), \
        "首次绑定必须互斥排除其它账号节点（风控核心）"


@pytest.mark.asyncio
async def test_rebalance_gateway_abort_aligns_binding(tp_mode, monkeypatch):
    """2.7.68 网关级熔断：连续候选探针连接级失败 = 本地网关故障，rebalance
    中止且不再 churn；退出前 hub 记录经 TP 快照对齐实际绑定（分叉=互斥集合
    被污染）。"""
    seq = {"probe": 0}

    async def fake_tp_ban(node, reason, evidence, account_name):
        pass

    async def fake_tp_acquire(account, force_rebalance=False, extra_exclude=None):
        seq["probe"] += 1
        return {"node_name": f"node-cand-{seq['probe']}"}

    async def fake_probe(self, account, proxy_url, jwt):
        return 0, "ConnectError: dead"  # 全部候选连接级失败（网关死的形态）

    async def fake_snapshot():
        # TP 侧已完成 2 次 force_rebalance，实际绑定在第 2 候选
        return {"zh-acc-r1": "node-cand-2"}, set()

    monkeypatch.setattr(egress_pool, "tp_ban", fake_tp_ban)
    monkeypatch.setattr(egress_pool, "tp_acquire", fake_tp_acquire)
    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", fake_snapshot)
    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", fake_probe)

    acc = _account(acc_id="acc-r1", name="r1")
    acc.egress_node = "node-old"

    ok = await egress_pool.egress_pool.rebalance(acc, reason="health_probe_fail", ban=False)
    assert ok is False  # 熔断 = 未达成健康换绑
    assert acc.egress_node == "node-cand-2", \
        "hub 记录必须对齐 TP 实际绑定（分叉 = 互斥集合被污染）"


@pytest.mark.asyncio
async def test_rebalance_recovers_after_all_rounds_fail(tp_mode, monkeypatch):
    """熔断关闭（TP_GATEWAY_FAIL_ABORT=0）时的既有行为回归：rebalance 3 轮
    全败——服务端绑定已随 force_rebalance 移走——第 4 轮 acquire+探针验收，
    hub 记录必须对齐 TP 实际绑定（不留在旧节点）。"""
    seq = {"probe": 0}

    async def fake_tp_ban(node, reason, evidence, account_name):
        pass

    async def fake_tp_acquire(account, force_rebalance=False, extra_exclude=None):
        return {"node_name": f"node-cand-{seq['probe']}"}

    async def fake_probe(self, account, proxy_url, jwt):
        seq["probe"] += 1
        return 0, "ConnectError: dead"  # 前三轮全部探不通

    monkeypatch.setattr(settings, "TP_GATEWAY_FAIL_ABORT", 0)
    monkeypatch.setattr(egress_pool, "tp_ban", fake_tp_ban)
    monkeypatch.setattr(egress_pool, "tp_acquire", fake_tp_acquire)
    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", fake_probe)

    acc = _account(acc_id="acc-r1", name="r1")
    acc.egress_node = "node-old"

    ok = await egress_pool.egress_pool.rebalance(acc, reason="health_probe_fail", ban=False)
    assert ok is False  # 未达成健康换绑
    assert acc.egress_node != "node-old", \
        "hub 记录不得停留在已被 force_rebalance 移走的旧节点（分叉=互斥集合被污染）"
    assert acc.egress_node == "node-cand-3"  # TP 实际绑定（第 3 候选）


def tmp_db_factory(monkeypatch):
    import pathlib
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp(prefix="egress-test-"))
    monkeypatch.setattr(settings, "DB_PATH", d / "test-accounts.db")
    return d / "test-accounts.db"


# ── 2026-11-16 trial-mvxmwyrg 事故回归（假换绑 + 探针盲区）──────────────────

@pytest.mark.asyncio
async def test_rebalance_excludes_own_old_node(tp_mode, monkeypatch):
    """假换绑修复（2026-11-16 生产实证 070→070）：ban=False 路径的
    exclude_node 必须携带本账号要离开的旧节点，force_rebalance 不得把
    绑定换回原节点。"""
    seen_exclude = []

    async def _spy_acquire(account, force_rebalance=False, extra_exclude=None):
        seen_exclude.append(list(extra_exclude or []))
        return {"node_name": "cand-new", "exit_ip": "1.2.3.4"}

    acc = _account(acc_id="acc-old")
    acc.egress_node = "my-dead-node"
    monkeypatch.setattr(egress_pool, "tp_acquire", _spy_acquire)
    monkeypatch.setattr(egress_pool, "_other_nodes",
                        lambda account: ["other-account-node"])
    monkeypatch.setattr(egress_pool, "tp_ban", lambda *a, **k: None)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)

    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="node_unreachable", ban=False)

    assert ok is True
    assert "my-dead-node" in seen_exclude[0], \
        "旧节点必须进 exclude_node（否则 TP 会粘性返回原节点 = 假换绑）"
    assert "other-account-node" in seen_exclude[0]  # 互斥集合不受影响


@pytest.mark.asyncio
async def test_rebalance_round_rejected_candidates_stay_excluded(tp_mode, monkeypatch):
    """轮内探针否决的候选不得在后续轮次被再次 acquire（防 3 轮打转）。"""
    seen_exclude = []

    async def _spy_acquire(account, force_rebalance=False, extra_exclude=None):
        seen_exclude.append(list(extra_exclude or []))
        return {"node_name": f"cand-{len(seen_exclude)}", "exit_ip": "1.2.3.4"}

    async def _async_ban(node, reason, evidence, account_name):
        pass

    acc = _account(acc_id="acc-cyc")
    acc.egress_node = "old-node"
    monkeypatch.setattr(egress_pool, "tp_acquire", _spy_acquire)
    monkeypatch.setattr(egress_pool, "_other_nodes", lambda account: [])
    monkeypatch.setattr(egress_pool, "tp_ban", _async_ban)

    async def _probe_html(self, account, proxy_url, jwt):
        return 405, "<!doctype html>405"  # 每个候选都被边缘封禁否决

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_html)

    pool = egress_pool.TetherPoolEgress()
    await pool.rebalance(acc, reason="edge_405_html", ban=False)

    # cand-1 被第 1 轮否决后，第 2 轮 exclude 必须含它
    assert "cand-1" in seen_exclude[1]


@pytest.mark.asyncio
async def test_conn_fail_cluster_forces_rebalance_despite_probe_ok(tp_mode, monkeypatch, tmp_path):
    """探针盲区收口（2026-11-16 trial-mvxmwyrg 18 连断事故）：半死节点能答
    小探针却断真实流式请求——聚类达阈值后无论探针结果强制 rebalance。"""

    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test-accounts.db")
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)

    acc = store.add_account("zai", "h1", "h.eyJzdWIiOiJ1In0.sig")
    acc.enabled = True
    acc.egress_node = "half-dead-node"

    rebalanced = {"n": 0, "reasons": []}

    async def fake_rebalance_locked(account, reason, ban=True):
        # verify_then_rebalance 已持 _account_lock（不可重入），
        # 故 stub 到 _rebalance_locked 层
        rebalanced["n"] += 1
        rebalanced["reasons"].append(reason)

    async def fake_health_once(account):
        return True  # 探针一直通（半死节点指纹）

    monkeypatch.setattr(egress_pool.egress_pool, "_rebalance_locked",
                        fake_rebalance_locked)
    monkeypatch.setattr(egress_pool.egress_pool, "health_once", fake_health_once)

    tag = egress.messages_egress_tag(acc)
    egress.clear_conn_fails(tag)
    try:
        # 聚类未达阈值：探针通 → 保持绑定（瞬态语义不变）
        await egress_pool.egress_pool.verify_then_rebalance(acc, reason="node_unreachable")
        assert rebalanced["n"] == 0

        # 达到聚类阈值：探针通也不得保持绑定
        for _ in range(egress.CONN_FAIL_THRESHOLD):
            egress.note_conn_fail(tag)
        await egress_pool.egress_pool.verify_then_rebalance(acc, reason="node_unreachable")
        assert rebalanced["n"] == 1
        # 聚类窗口已清（换绑后新节点干净开局）
        assert not egress.conn_fail_clustered(tag)
    finally:
        egress.clear_conn_fails(tag)


def test_messages_egress_tag_matches_gateway_helper(tp_mode):
    """单一真相源：egress.messages_egress_tag 与 gateway._messages_egress
    必须同构（2026-11-16 收口——两处独立实现曾致聚类 key 漂移）。"""
    from app.routes import gateway as gw

    acc = _account(acc_id="acc-t")
    assert gw._messages_egress(acc) == egress.messages_egress_tag(acc)


# ── 2026-11-18 1117 全灭复盘：alicdn 路由维度（2.7.62）─────────────────────

def test_probe_alicdn_fast_ok(monkeypatch):
    """200 + 快速 → 合格。"""
    monkeypatch.setattr(egress_pool, "probe_alicdn", _REAL_PROBE_ALICDN)
    ok, elapsed, detail = asyncio.run(
        egress_pool.probe_alicdn("http://gw", fetch_impl=lambda u, t, f: (200, "ok")))
    assert ok is True and detail == "ok"


def test_probe_alicdn_direct_passes_without_fetch(monkeypatch):
    """proxy_url 为空（直连形态）恒过，且不得发起网络调用。"""
    monkeypatch.setattr(egress_pool, "probe_alicdn", _REAL_PROBE_ALICDN)
    def _boom(url, timeout, full=False):
        raise AssertionError("直连形态不应发起 alicdn 探针")
    ok, elapsed, detail = asyncio.run(
        egress_pool.probe_alicdn(None, fetch_impl=_boom))
    assert ok is True and detail == "direct-no-probe"


def test_probe_alicdn_http_fail_rejected(monkeypatch):
    monkeypatch.setattr(egress_pool, "probe_alicdn", _REAL_PROBE_ALICDN)
    ok, _elapsed, detail = asyncio.run(
        egress_pool.probe_alicdn("http://gw", fetch_impl=lambda u, t, f: (0, "SSLError: eof")))
    assert ok is False and "SSLError" in detail


def test_probe_alicdn_slow_rejected(monkeypatch):
    """超 EGRESS_ALICDN_MAX_SECONDS = 拒收（1117 实测 9.9~31.9s 路由劣化）。"""
    monkeypatch.setattr(egress_pool, "probe_alicdn", _REAL_PROBE_ALICDN)
    monkeypatch.setattr(settings, "EGRESS_ALICDN_MAX_SECONDS", 0.05)

    def _slow(url, timeout, full=False):
        time.sleep(0.12)
        return 200, "ok"

    ok, elapsed, detail = asyncio.run(
        egress_pool.probe_alicdn("http://gw", fetch_impl=_slow))
    assert ok is False and "slow" in detail


@pytest.mark.asyncio
async def test_rebalance_rejects_alicdn_slow_candidate_without_ban(tp_mode, monkeypatch):
    """zcode 探针放行但 alicdn 路由不合格 → 换下一个候选且不得 ban（路由差≠信誉烂）。"""
    stub = _StubPool(
        [{"node_name": "cand-slow", "exit_ip": "1.2.3.4"},
         {"node_name": "cand-fast", "exit_ip": "5.6.7.8"}],
        [(405, '{"code":3012}'), (405, '{"code":3012}')],
    )
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)

    async def _slow_first(proxy_url, *, full=False, fetch_impl=None):
        # 覆盖 autouse 恒过 fixture：第 1 候选 alicdn 不合格，其后合格
        if not getattr(_slow_first, "used", False):
            _slow_first.used = True
            return False, 21.0, "slow 21.0s > 5s"
        return True, 1.2, "ok"
    monkeypatch.setattr(egress_pool, "probe_alicdn", _slow_first)

    acc = _account(acc_id="acc-cdn")
    acc.egress_node = "old-node"
    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=True)

    assert ok is True
    assert acc.egress_node == "cand-fast"
    # alicdn 慢的候选绝不进 ban 名单（只隔离信誉烂的）
    assert stub.bans == ["old-node"]


@pytest.mark.asyncio
async def test_health_once_gates_on_alicdn(tp_mode, monkeypatch):
    """健康判定新增 alicdn 维度：zcode 探针通但 alicdn 路由烂 = 不健康。"""
    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)

    acc = _account(acc_id="acc-h")
    acc.egress_node = "node-h"
    pool = egress_pool.TetherPoolEgress()

    async def _bad(proxy_url, *, full=False, fetch_impl=None):
        return False, 17.0, "slow 17.0s > 5s"
    monkeypatch.setattr(egress_pool, "probe_alicdn", _bad)
    assert await pool.health_once(acc) is False

    async def _good(proxy_url, *, full=False, fetch_impl=None):
        return True, 1.2, "ok"
    monkeypatch.setattr(egress_pool, "probe_alicdn", _good)
    assert await pool.health_once(acc) is True


# ── 2026-11-18 review P1 根修:TP 实时对账冲突拒收(2.7.63)──────────────────

def _live_snapshot(allocs: dict[str, str], banned: set[str]):
    async def _snap():
        return allocs, banned
    return _snap


@pytest.mark.asyncio
async def test_rebalance_rejects_same_node_return(tp_mode, monkeypatch):
    """假换绑 acceptance 侧根修（2026-11-18 review P1#2）：TP 池压下无视
    exclude_node 粘性返回原节点（6154→6154 实锤）——候选 == 旧节点必须视为
    本轮失败换下一个，探针放行不得收货。"""
    stub = _StubPool(
        [{"node_name": "old-node", "exit_ip": "1.1.1.1"},   # 假换绑返回
         {"node_name": "cand-new", "exit_ip": "2.2.2.2"}],  # 真新节点
        [(405, '{"code":3012}'), (405, '{"code":3012}')],
    )
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)
    monkeypatch.setattr(egress_pool, "_other_nodes", lambda account: [])

    acc = _account(acc_id="acc-same")
    acc.egress_node = "old-node"
    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=True)

    assert ok is True
    assert acc.egress_node == "cand-new"
    # 旧节点在第 1 轮被 ban（ban=True 语义不变），假换绑候选不再被「确认成功」
    assert stub.bans == ["old-node"]
    assert stub.acquired[0]["exclude"] == ["old-node"]


@pytest.mark.asyncio
async def test_rebalance_rejects_banned_node_candidate(tp_mode, monkeypatch):
    """TP avoid_banned 兜底破约（2026-11-18 实锤：zcodereg3 绑上在期 ban 的
    542）——收货前反查在期隔离台账，命中即换下一个候选。"""
    stub = _StubPool(
        [{"node_name": "clash-banned-1", "exit_ip": "3.3.3.3"},
         {"node_name": "cand-ok", "exit_ip": "4.4.4.4"}],
        [(405, '{"code":3012}'), (405, '{"code":3012}')],
    )
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)
    monkeypatch.setattr(egress_pool, "_other_nodes", lambda account: [])
    monkeypatch.setattr(egress_pool, "_tp_live_snapshot",
                        _live_snapshot({}, {"clash-banned-1"}))

    acc = _account(acc_id="acc-ban")
    acc.egress_node = "old-node"
    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=False)

    assert ok is True
    assert acc.egress_node == "cand-ok"
    assert "clash-banned-1" in stub.acquired[1]["exclude"]  # 否决候选进 exclude


@pytest.mark.asyncio
async def test_rebalance_rejects_node_held_by_other_allocation(tp_mode, monkeypatch):
    """互斥破约实锤（2026-11-18 实查：zcodereg4/5 同占 clash-6157）——候选
    节点被本 service 其它 zh-* 分配现占时必须换下一个。"""
    stub = _StubPool(
        [{"node_name": "clash-shared", "exit_ip": "5.5.5.5"},
         {"node_name": "cand-solo", "exit_ip": "6.6.6.6"}],
        [(405, '{"code":3012}'), (405, '{"code":3012}')],
    )
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)
    monkeypatch.setattr(egress_pool, "_other_nodes", lambda account: [])
    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _live_snapshot(
        {"zh-other-account-1": "clash-shared",
         "zh-acc-dup": "clash-elsewhere"},  # 候选被别的分配占着
        set()))

    acc = _account(acc_id="acc-dup")
    acc.egress_node = "old-node"
    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=False)

    assert ok is True
    assert acc.egress_node == "cand-solo"


@pytest.mark.asyncio
async def test_rebalance_snapshot_failure_fails_open(tp_mode, monkeypatch):
    """对账快照拉取失败 = 跳过冲突检查（fail-open），rebalance 语义退回
    2.7.62 前行为——绝不因控制面抖动而拒绝一切候选。"""
    async def _dead_snapshot():
        return {}, set()

    stub = _StubPool([{"node_name": "cand-x", "exit_ip": "7.7.7.7"}],
                     [(405, '{"code":3012}')])
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)
    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _dead_snapshot)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)
    monkeypatch.setattr(egress_pool, "_other_nodes", lambda account: [])

    acc = _account(acc_id="acc-open")
    acc.egress_node = "old-x"  # 快照坏 → 隔离/互斥检查跳过（fail-open）→ 仍收货 cand-x
    pool = egress_pool.TetherPoolEgress()
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=False)
    assert ok is True


@pytest.mark.asyncio
async def test_rebalance_acceptance_uses_full_alicdn_probe(tp_mode, monkeypatch):
    """alicdn 双档（2.7.63 review P2#9）：换绑验收用整文件强探针（full=True），
    健康循环保持 64KB 轻探针——头部快 ≠ 整文件快（trial 6154 全文件 10.2s
    却过 64KB 采样实锤）。"""
    seen: dict = {}

    async def _recorder(proxy_url, *, full=False, fetch_impl=None):
        seen.setdefault("fulls", []).append(full)
        return True, 0.5, "ok"

    monkeypatch.setattr(egress_pool, "probe_alicdn", _recorder)

    stub = _StubPool([{"node_name": "cand-full", "exit_ip": "8.8.8.8"}],
                     [(405, '{"code":3012}')])
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)
    monkeypatch.setattr(egress_pool, "_other_nodes", lambda account: [])

    acc = _account(acc_id="acc-full")
    pool = egress_pool.TetherPoolEgress()
    await pool.rebalance(acc, reason="edge_405_html", ban=False)
    assert seen["fulls"] == [True]  # 验收路径 = 整文件档


@pytest.mark.asyncio
async def test_health_once_uses_light_alicdn_probe(tp_mode, monkeypatch):
    seen: dict = {}

    async def _recorder(proxy_url, *, full=False, fetch_impl=None):
        seen["full"] = full
        return True, 0.5, "ok"

    monkeypatch.setattr(egress_pool, "probe_alicdn", _recorder)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)

    acc = _account(acc_id="acc-light")
    pool = egress_pool.TetherPoolEgress()
    assert await pool.health_once(acc) is True
    assert seen["full"] is False  # 健康循环 = 64KB 轻档


@pytest.mark.asyncio
async def test_delete_account_handler_releases_tp_allocation(monkeypatch, tp_mode):
    """账号删除必须释放 TP 分配（2026-11-18 review P2#4：16/27 陈旧分配即
    历史删除账号残留）——真实 handler 路径：delete_accounts → store.remove
    → tp_release_account(zh-<id>)。"""
    from app.routes import admin_api as admin

    class _FakeStore:
        def __init__(self, acc):
            self.acc = acc
            self.removed: list[str] = []

        def find_any(self, aid):
            return self.acc if self.acc and self.acc.id == aid else None

        def remove_account(self, provider, aid):
            self.removed.append(aid)
            return True

    fake = _FakeStore(_account(acc_id="acc-del"))
    monkeypatch.setattr(admin, "store", fake)
    released: list[str] = []

    async def fake_release(account_id: str) -> bool:
        released.append(account_id)
        return True

    monkeypatch.setattr(egress_pool, "tp_release_account", fake_release)

    out = await admin.delete_accounts(["acc-del"])
    assert out == {"deleted": 1}
    assert fake.removed == ["acc-del"]
    assert released == ["acc-del"]


# ── 2.7.65 review 收尾批次 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_tp_acquire_sends_tier_param(monkeypatch, tp_mode):
    """tier 承诺必须显式上送（2026-11-21）：缺失时 IP-Pool 按 standard_first
    默认策略把机房 standard 节点粘给 zcode 号——与 gateway URL 的 __tier_
    username 同源（ZCODE_TP_TIER）。"""
    calls: list[str] = []

    def _fake_http(method, url, body=None, headers=None, timeout=15):
        calls.append(url)
        return 200, '{"node_name": "clash-301-pigeonw-01"}', {}

    monkeypatch.setattr(egress_pool, "_tp_http", _fake_http)
    d = await egress_pool.tp_acquire(_account(acc_id="acc-tier"))
    assert d and d["node_name"] == "clash-301-pigeonw-01"
    assert len(calls) == 1
    # tp_mode 夹具钉 TP_TIER=standard——断言 params 与 env 同源即可
    assert "tier=standard" in calls[0]


@pytest.mark.asyncio
async def test_release_account_success_false_is_idempotent_ok(monkeypatch, tp_mode):
    """HTTP 200 + body success:false = 无分配可释（幂等成功），必须返回 True
    且不落 warn（2026-11-18 review P3：此前只看状态码）。"""
    calls: list[tuple] = []

    def _fake_http(method, url, body=None, headers=None, timeout=15):
        calls.append((method, url))
        return 200, '{"success": false, "error": "no allocation"}', {}

    monkeypatch.setattr(egress_pool, "_tp_http", _fake_http)
    ok = await egress_pool.tp_release_account("acc-rel")
    assert ok is True
    assert len(calls) == 1 and "release" in calls[0][1]


@pytest.mark.asyncio
async def test_release_account_non_dict_json_is_idempotent_ok(monkeypatch, tp_mode):
    """review 复核 P3-1：release 返回非 dict JSON（数组/字符串）不得崩，
    按「无法判读」处理为幂等成功（调用方语义：释放尽力而为）。"""
    for raw in ('[1,2]', '"ok"', 'null'):
        def _fake_http(method, url, body=None, headers=None, timeout=15, _raw=raw):
            return 200, _raw, {}
        monkeypatch.setattr(egress_pool, "_tp_http", _fake_http)
        assert await egress_pool.tp_release_account("acc-nondict") is True, raw


@pytest.mark.asyncio
async def test_release_account_http_500_returns_false(monkeypatch, tp_mode):
    def _fake_http(method, url, body=None, headers=None, timeout=15):
        return 500, "boom", {}

    monkeypatch.setattr(egress_pool, "_tp_http", _fake_http)
    assert await egress_pool.tp_release_account("acc-rel2") is False


@pytest.mark.asyncio
async def test_snapshot_accepts_aware_and_naive_quarantine(monkeypatch, tp_mode):
    """quarantined_until 带 tzinfo（aware）不得 TypeError 崩快照（review P3）：
    aware/naive 都归一后正确入隔离集；过期/坏格式跳过。"""
    import datetime as _dt

    future_aware = (_dt.datetime.now().astimezone()
                    + _dt.timedelta(hours=1)).isoformat()
    past_aware = (_dt.datetime.now().astimezone()
                  - _dt.timedelta(hours=1)).isoformat()
    payload = [
        {"node_name": "n-aware-future", "quarantined_until": future_aware},
        {"node_name": "n-aware-past", "quarantined_until": past_aware},
        {"node_name": "n-naive-future",
         "quarantined_until": (_dt.datetime.now() + _dt.timedelta(hours=1)).isoformat()},
        {"node_name": "n-garbage", "quarantined_until": "not-a-date"},
        {"node_name": "n-null", "quarantined_until": None},
    ]

    def _fake_http(method, url, body=None, headers=None, timeout=15):
        if "/feedback/bans" in url:
            return 200, json.dumps(payload), {}
        return 200, "[]", {}

    monkeypatch.setattr(egress_pool, "_tp_http", _fake_http)
    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _REAL_TP_SNAPSHOT)
    allocs, banned = await egress_pool._tp_live_snapshot()
    assert "n-aware-future" in banned
    assert "n-naive-future" in banned
    assert "n-aware-past" not in banned
    assert "n-garbage" not in banned and "n-null" not in banned
    assert allocs == {}


@pytest.mark.asyncio
async def test_main_loop_post_acceptance_recheck_rejects_holder(tp_mode, monkeypatch):
    """P2#1 收货复核：验收窗口内台账被其它账号占用（复核快照 != 收货快照）
    → 候选拒绝换下一个。"""
    acc = _account(acc_id="acc-recheck")
    acc.egress_node = "old-node"
    stub = _StubPool(
        [{"node_name": "cand-a", "exit_ip": "1.2.3.4"},
         {"node_name": "cand-b", "exit_ip": "1.2.3.5"}],
        [(405, '{"code":3012}')],  # 主循环候选探针恒 JSON-APP
    )
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)

    # 收货快照空（fail-open 放行探针链）→ 复核快照发现 cand-a 已被他人占用
    snapshots = iter([
        ({}, set()),                                  # 收货快照（rebalance 开头）
        ({"zh-other": "cand-a"}, set()),              # 候选 cand-a 收货复核
        ({}, set()),                                  # 候选 cand-b 收货复核
    ])

    async def _seq_snapshot():
        return next(snapshots)

    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _seq_snapshot)

    ok = await egress_pool.egress_pool.rebalance(acc, reason="test", ban=False)
    assert ok is True
    assert acc.egress_node == "cand-b"  # cand-a 被复核拒绝，落在 cand-b


@pytest.mark.asyncio
async def test_round4_conflict_short_circuits_before_probes(tp_mode, monkeypatch):
    """review P3：第 4 轮候选命中复核冲突 → 不烧探针直接判败。"""
    acc = _account(acc_id="acc-r4sc")
    acc.egress_node = "old-node"

    async def _fake_acquire(account, force_rebalance=False, extra_exclude=None):
        return {"node_name": f"cand-{len(extra_exclude or [])}"}

    probe_calls: list[int] = []

    async def _probe(self, account, proxy_url, jwt):
        probe_calls.append(1)
        return 0, '{"code":3012}'

    async def _noop_ban(node, reason, evidence, account_name):
        pass

    monkeypatch.setattr(egress_pool, "tp_acquire", _fake_acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", _noop_ban)
    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe)
    # 本测试探针桩返回 st=0（连接级失败）——2.7.68 起这会触发网关级熔断；
    # 此处关注第 4 轮短路语义，显式关闭熔断（熔断另有专测）
    monkeypatch.setattr(settings, "TP_GATEWAY_FAIL_ABORT", 0)

    # 复核快照恒返回 cand-* 全部被 zh-other 占用
    async def _conflict_snapshot():
        return {"zh-other": "cand-4"}, set()

    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _conflict_snapshot)

    ok = await egress_pool.egress_pool.rebalance(acc, reason="test", ban=False)
    assert ok is False
    assert len(probe_calls) == 3, \
        "主循环 3 轮探针合法；第 4 轮复核冲突候选不得烧探针"


@pytest.mark.asyncio
async def test_round4_same_node_degrade_accept_on_fresh_acceptance(tp_mode, monkeypatch):
    """P2#2 单节点池退化：4 轮全为假换绑（TP 粘性返回原节点）+ 不在隔离台账
    + 无他人占用 + 全新全量验收通过 → 降级接受原节点并恢复服务。"""
    acc = _account(acc_id="acc-degrade")
    acc.egress_node = "only-node"

    async def _fake_acquire(account, force_rebalance=False, extra_exclude=None):
        return {"node_name": "only-node", "exit_ip": "5.6.7.8"}

    async def _probe(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'  # zcode 探针 JSON-APP 放行

    async def _ok_cd(proxy_url, *, full=False, fetch_impl=None):
        assert full is True, "降级验收必须整文件档"
        return True, 1.0, "ok"

    async def _noop_ban(node, reason, evidence, account_name):
        pass

    # 复核快照干净：only-node 既不在隔离台账也无他人占用（ban=False 路径）
    async def _clean_snapshot():
        return {"zh-acc-degrade": "only-node"}, set()

    monkeypatch.setattr(egress_pool, "tp_acquire", _fake_acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", _noop_ban)
    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe)
    monkeypatch.setattr(egress_pool, "probe_alicdn", _ok_cd)
    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _clean_snapshot)

    ok = await egress_pool.egress_pool.rebalance(acc, reason="test", ban=False)
    assert ok is True, "池内无替代节点 + 全新验收通过应降级接受"
    assert acc.egress_node == "only-node"


@pytest.mark.asyncio
async def test_round4_same_node_degrade_rejected_when_banned(tp_mode, monkeypatch):
    """P2#2 反例：ban=True 时原节点被自己 ban 进台账 → 复核冲突判败，
    绝不降级接受刚隔离的节点。"""
    acc = _account(acc_id="acc-degrade2")
    acc.egress_node = "only-node"

    async def _fake_acquire(account, force_rebalance=False, extra_exclude=None):
        return {"node_name": "only-node", "exit_ip": "5.6.7.8"}

    probe_calls: list[int] = []

    async def _probe(self, account, proxy_url, jwt):
        probe_calls.append(1)
        return 405, '{"code":3012}'

    async def _noop_ban(node, reason, evidence, account_name):
        pass

    # 复核快照：only-node 在期隔离（自家 ban 生效）
    async def _banned_snapshot():
        return {}, {"only-node"}

    monkeypatch.setattr(egress_pool, "tp_acquire", _fake_acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", _noop_ban)
    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe)
    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _banned_snapshot)

    ok = await egress_pool.egress_pool.rebalance(acc, reason="test", ban=True)
    assert ok is False
    assert acc.egress_node == "only-node"  # hub 记录对齐 TP 实际绑定


def test_alicdn_full_fetch_caps_oversize(monkeypatch, tp_mode):
    """review P3：full=True 整文件读封顶 4MB——异常超大响应判不合格而非读爆。"""

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            return b"x" * (size if size and size > 0 else 65536)

        def close(self):
            pass

    class _FakeOpener:
        def open(self, req, timeout=None):
            return _FakeResp()

    monkeypatch.setattr(egress_pool, "_opener", lambda proxy: _FakeOpener())
    st, detail = egress_pool._alicdn_fetch("http://gw", 5.0, full=True)
    assert st == 0 and "oversize" in detail


@pytest.mark.asyncio
async def test_delete_gate_skips_graylist_excluded_account(monkeypatch, tp_mode):
    """review Suggestion：灰度名单非空且账号不在名单 → 从未持有 TP 分配，
    删除路径不得调 release。"""
    from app.routes import admin_api as admin

    monkeypatch.setattr(settings, "TP_GRAY_ACCOUNTS", ["some-other-account"])

    class _FakeStore:
        def __init__(self, acc):
            self.acc = acc

        def find_any(self, aid):
            return self.acc if self.acc and self.acc.id == aid else None

        def remove_account(self, provider, aid):
            return True

    fake = _FakeStore(_account(acc_id="acc-gray"))
    monkeypatch.setattr(admin, "store", fake)
    released: list[str] = []

    async def fake_release(account_id: str) -> bool:
        released.append(account_id)
        return True

    monkeypatch.setattr(egress_pool, "tp_release_account", fake_release)

    out = await admin.delete_accounts(["acc-gray"])
    assert out == {"deleted": 1}
    assert released == []  # 不在灰度名单 = 不释放（从未分配）


@pytest.mark.asyncio
async def test_rebalance_debounce_stamped_at_end(tp_mode, monkeypatch):
    """2.7.68 去抖戳 = 本轮收尾时刻：成功 rebalance（2-3 分钟 > 120s 去抖）
    结束瞬间，下一个触发必须被去抖拦住（旧实现取入口/首轮 acquire 时刻，
    长轮一结束去抖窗已耗尽 = 故障期背靠背无限 churn）。"""
    stub = _StubPool(
        [{"node_name": "cand-1", "exit_ip": "1.2.3.4"}],
        [(405, '{"code":3012}')],
    )
    monkeypatch.setattr(egress_pool, "tp_acquire", stub.acquire)
    monkeypatch.setattr(egress_pool, "tp_ban", stub.ban)

    async def _probe_ok(self, account, proxy_url, jwt):
        return 405, '{"code":3012}'  # JSON-APP：候选验收放行

    monkeypatch.setattr(egress_pool.TetherPoolEgress, "_probe_via", _probe_ok)

    async def _empty_snapshot():
        return {}, set()

    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", _empty_snapshot)
    acc = _account(acc_id="acc-z")
    acc.egress_node = "old-node"

    pool = egress_pool.TetherPoolEgress()
    monkeypatch.setattr(settings, "TP_REBALANCE_DEBOUNCE", 120)
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=False)
    assert ok is True
    first_end = pool._rebalance_at[acc.id]
    assert first_end > 0
    # 立即第二次触发：去抖窗内（戳是收尾时刻）→ 跳过
    ok2 = await pool.rebalance(acc, reason="edge_405_html", ban=False)
    assert ok2 is False


@pytest.mark.asyncio
async def test_reconcile_orphans_releases_stale_allocations(tp_mode, monkeypatch):
    """2.7.68 启动对账：allocs 里 zh-<id> 不在本库账号集合 → release；
    在库 id 与非 zh- 前缀不动。"""

    released: list[str] = []

    async def fake_release(account_id: str) -> bool:
        released.append(account_id)
        return True

    async def fake_snapshot():
        return {"zh-live-1": "node-a", "zh-stale-old": "node-b",
                "other-service-acc": "node-c"}, set()

    class _FakeStore:
        @staticmethod
        def list_accounts(provider):
            return [_account(acc_id="live-1")]

    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", fake_snapshot)
    monkeypatch.setattr(egress_pool, "tp_release_account", fake_release)
    import app.egress_pool as _ep
    monkeypatch.setattr(_ep, "PROVIDERS", ("zai",))
    monkeypatch.setattr("app.store.store.list_accounts",
                        lambda provider: [_account(acc_id="live-1")])

    pool = egress_pool.egress_pool
    await pool._reconcile_once(initial_delay=0)
    assert released == ["stale-old"]


@pytest.mark.asyncio
async def test_reconcile_aligns_drifted_record(tp_mode, monkeypatch):
    """2026-11-21 存量巡检②：hub egress_node ≠ TP 实际绑定（IP-Pool 侧
    auto_rehome 主动换绑）→ hub 记录对齐地面真值，不做 rebalance。"""
    updated: list[str] = []

    async def fake_snapshot():
        return {"zh-live-1": "tp-real-node"}, set()

    acc = _account(acc_id="live-1")
    acc.egress_node = "hub-stale-node"

    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", fake_snapshot)
    monkeypatch.setattr(egress_pool, "tp_release_account", AsyncMock(return_value=True))
    monkeypatch.setattr(egress_pool, "PROVIDERS", ("zai",))
    monkeypatch.setattr("app.store.store.list_accounts", lambda provider: [acc])

    class _SpyStore:
        @staticmethod
        def list_accounts(provider):
            return [acc]

        @staticmethod
        def update_account(a):
            updated.append(a.egress_node)

    import app.store as store_mod2
    monkeypatch.setattr(store_mod2, "store", _SpyStore)
    # 防御：rebalance 误触发会经这里
    rb: list = []

    async def _no_rebalance(account, reason, ban=True):
        rb.append(reason)
        return True

    monkeypatch.setattr(egress_pool.egress_pool, "rebalance", _no_rebalance)

    await egress_pool.egress_pool._reconcile_once(initial_delay=0)
    assert acc.egress_node == "tp-real-node"
    assert updated == ["tp-real-node"]
    assert rb == []  # 记录漂移只对齐，不换绑


@pytest.mark.asyncio
async def test_reconcile_rebalances_co_occupied_node(tp_mode, monkeypatch):
    """2026-11-21 存量巡检③：TP 实际绑定与其它账号分配同节点（软语义降级/
    auto_rehome 制造的存量共占）→ 强制 rebalance 回收互斥。"""
    async def fake_snapshot():
        return {"zh-live-1": "shared-node", "zh-live-2": "shared-node",
                "zh-live-3": "own-node"}, set()

    a1 = _account(acc_id="live-1", name="a1")
    a1.egress_node = "shared-node"
    a2 = _account(acc_id="live-2", name="a2")
    a2.egress_node = "shared-node"
    a3 = _account(acc_id="live-3", name="a3")
    a3.egress_node = "own-node"

    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", fake_snapshot)
    monkeypatch.setattr(egress_pool, "tp_release_account", AsyncMock(return_value=True))
    monkeypatch.setattr(egress_pool, "PROVIDERS", ("zai",))
    monkeypatch.setattr("app.store.store.list_accounts",
                        lambda provider: [a1, a2, a3])

    rebalanced: list[str] = []

    async def _spy_rebalance(account, reason, ban=True):
        rebalanced.append((account.name, reason))
        return True

    monkeypatch.setattr(egress_pool.egress_pool, "rebalance", _spy_rebalance)

    await egress_pool.egress_pool._reconcile_once(initial_delay=0)
    # 两个共占号都换绑，独占号不动
    assert sorted(rebalanced) == [("a1", "co_occupancy_reconcile"),
                                  ("a2", "co_occupancy_reconcile")]


@pytest.mark.asyncio
async def test_reconcile_loop_sweeps_periodically(tp_mode, monkeypatch):
    """2026-11-21 迭代 review P2：巡检周期化——IP-Pool 数据面 auto_rehome /
    gateway persist 随时制造新共占，reconcile 不再是启动单次。loop 按
    TP_RECONCILE_INTERVAL 周期跑 sweep；异常只 warn 不退出。"""
    sweeps = []
    booms = []

    async def fake_sweep():
        sweeps.append(1)

    async def _boom():
        booms.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(settings, "TP_RECONCILE_INTERVAL", 0)  # 最短周期
    monkeypatch.setattr(egress_pool.egress_pool, "_reconcile_sweep", fake_sweep)

    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "tetherpool")
    loop_task = asyncio.create_task(egress_pool.egress_pool._reconcile_loop())
    await asyncio.sleep(0.1)
    assert len(sweeps) >= 2  # interval=0 -> 连跑多轮
    # 异常不退出：换上抛异常版本后 boom 必须真的发生过（异常路径可达），
    # 且循环仍在跑到下一轮——boom 计数 > 1 即证明 except 分支后继续 sleep/sweep
    n0 = len(sweeps)
    monkeypatch.setattr(egress_pool.egress_pool, "_reconcile_sweep", _boom)
    await asyncio.sleep(0.1)
    assert booms, "sweep 抛异常路径未被执行"
    assert len(booms) >= 2, "异常后循环退出——except 分支未兜住（review P3 回归）"
    assert len(sweeps) == n0, "异常轮不该计入成功 sweep"
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_reconcile_sweep_skips_empty_snapshot_with_warn(tp_mode, monkeypatch, caplog):
    """review P2：周期 sweep 快照为空（TP 故障 fail-open）→ 跳过整轮但必须
    打 warn 留痕——静默空转会让 TP 故障期的共占/漂移累积不可观测。"""
    warned: list = []

    async def fake_snapshot():
        return {}, set()  # TP 故障/空池

    def _fake_warn(src, msg):
        warned.append(msg)

    monkeypatch.setattr(egress_pool, "_tp_live_snapshot", fake_snapshot)
    import app.logs as logs_mod
    monkeypatch.setattr(logs_mod, "warn", _fake_warn)

    await egress_pool.egress_pool._reconcile_sweep()
    assert any("快照为空" in m for m in warned)


@pytest.mark.asyncio
async def test_start_idempotent_reconcile_loop(tp_mode, monkeypatch):
    """review P2：二次 start() 不得覆盖 _reconcile_loop_task 引用——否则
    旧 while-True 任务泄漏成孤儿（每 30min 空转直到进程退出）。"""
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "tetherpool")
    monkeypatch.setattr(settings, "TP_HEALTH_INTERVAL", 300)
    monkeypatch.setattr(settings, "TP_RECONCILE_INTERVAL", 300)

    pool = egress_pool.egress_pool
    saved = []
    real_loop = pool._reconcile_loop

    async def _spy_loop():
        saved.append(id(asyncio.current_task()))
        await real_loop()

    monkeypatch.setattr(pool, "_reconcile_loop", _spy_loop)
    pool.start()
    try:
        first = pool._reconcile_loop_task
        assert first is not None
        pool.start()  # 二次调用
        assert pool._reconcile_loop_task is first, \
            "二次 start 覆盖了巡检循环引用——旧任务泄漏"
    finally:
        await pool.stop()
    assert pool._reconcile_loop_task is None


@pytest.mark.asyncio
async def test_tp_acquire_sends_exclusive_param(tp_mode, monkeypatch):
    """2026-11-21 硬互斥 opt-in：tp_acquire 必须带 exclusive=true（IP-Pool
    侧据此剔除本 service 其它账号现占节点），一号一节点从分配源头成立。"""
    captured: dict = {}

    def _fake_tp_http(method, url, body=None, headers=None, timeout=15):
        captured["url"] = url
        return 200, json.dumps({"node_name": "node-x", "exit_ip": "9.9.9.9"}), {}

    monkeypatch.setattr(egress_pool, "_tp_http", _fake_tp_http)
    acc = _account(acc_id="acc-e")
    d = await egress_pool.tp_acquire(acc)
    assert d and d.get("node_name") == "node-x"
    assert "exclusive=true" in captured["url"]


@pytest.mark.asyncio
async def test_listeners_supply_rebalance_gate(tp_mode, monkeypatch):
    """2.7.68 供给模式 gate：listeners 供给下 rebalance 不打 TP API（改绑定
    对 listener 出口毫无意义），干净返回 False——claim 换绑链据此单发止损。"""
    acquire_calls: list = []

    async def _spy_acquire(account, force_rebalance=False, extra_exclude=None):
        acquire_calls.append(1)
        return {"node_name": "cand-x", "exit_ip": "1.2.3.4"}

    monkeypatch.setattr(egress_pool, "tp_acquire", _spy_acquire)
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "listeners")  # 非 tetherpool

    acc = _account(acc_id="acc-ls")
    acc.egress_node = "stale-tp-node"
    acc.egress = "http://127.0.0.1:11031"

    pool = egress_pool.egress_pool
    ok = await pool.rebalance(acc, reason="edge_405_html", ban=True)
    assert ok is False
    assert acquire_calls == [], "listeners 供给下不得打 TP acquire"
    assert acc.egress_node == "stale-tp-node"  # 绑定不被 TP 污染


@pytest.mark.asyncio
async def test_ensure_binding_listeners_supply_no_tp(tp_mode, monkeypatch):
    """listeners 供给：绑定引导不打 TP，egress 已配即视为已绑定。"""
    acquire_calls: list = []

    async def _spy_acquire(account, force_rebalance=False, extra_exclude=None):
        acquire_calls.append(1)
        return {"node_name": "cand-x"}

    monkeypatch.setattr(egress_pool, "tp_acquire", _spy_acquire)
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "listeners")

    acc = _account(acc_id="acc-ls2")
    acc.egress = "http://127.0.0.1:11032"
    acc.egress_node = None  # 无 TP 节点（listeners 时代的合法形态）

    pool = egress_pool.egress_pool
    ok = await pool.ensure_binding(acc)
    assert ok is True
    assert acquire_calls == [], "listeners 供给下绑定引导不得打 TP"


# ── listeners 供给健康探针（2.7.69）──────────────────────────────────────────
@pytest.fixture
def listeners_mode(monkeypatch):
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "listeners")
    monkeypatch.setattr(settings, "TP_HEALTH_FAIL_THRESHOLD", 2)
    monkeypatch.setattr(settings, "TP_HEALTH_INTERVAL", 300)
    monkeypatch.setattr(settings, "CONNECT_FAIL_COOLDOWN", 60)


def _listener_account(acc_id="acc-lis", name="lis1"):
    acc = _account(acc_id=acc_id, name=name)
    acc.egress = "http://127.0.0.1:11031"
    acc.status = Status.ACTIVE
    return acc


@pytest.mark.asyncio
async def test_listener_health_cooldowns_after_streak(listeners_mode, monkeypatch, tmp_path):
    """探针连败达阈值 → 冷却一个健康周期（轮询绕开死出口）；连败期不重置
    streak，下轮探针仍败立即续冷（死出口不再进入可选窗口）。"""
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test-accounts.db")
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)
    acc = store.add_account("zai", "lis1", "h.eyJzdWIiOiJ1In0.sig")
    acc.enabled = True
    acc.egress = "http://127.0.0.1:11031"
    acc.status = Status.ACTIVE

    async def fail_probe(account):
        return False

    monkeypatch.setattr(egress_pool.egress_pool, "health_once", fail_probe)

    await egress_pool.egress_pool._check_listener(acc)  # 单败：只留痕
    assert acc.status == Status.ACTIVE
    await egress_pool.egress_pool._check_listener(acc)  # 连败 2：冷却
    assert acc.status == Status.COOLING
    assert acc.cooling_until is not None
    assert acc.id in egress_pool.egress_pool._listener_cooled
    cool_at = acc.cooling_until

    await egress_pool.egress_pool._check_listener(acc)  # 第 3 轮仍败：续冷（不重置 streak）
    assert acc.status == Status.COOLING
    assert acc.cooling_until >= cool_at
    assert egress_pool.egress_pool._probe_fail_streak.get(acc.id, 0) >= 2


@pytest.mark.asyncio
async def test_listener_health_recovers_on_green_probe(listeners_mode, monkeypatch, tmp_path):
    """冷却后探针转好 → streak 清零 + 解除本循环下的冷却复活选号。"""
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test-accounts.db")
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)
    acc = store.add_account("zai", "lis1", "h.eyJzdWIiOiJ1In0.sig")
    acc.enabled = True
    acc.egress = "http://127.0.0.1:11031"
    acc.status = Status.ACTIVE

    probe_ok = {"val": False}

    async def flip_probe(account):
        return probe_ok["val"]

    monkeypatch.setattr(egress_pool.egress_pool, "health_once", flip_probe)

    probe_ok["val"] = False
    await egress_pool.egress_pool._check_listener(acc)
    await egress_pool.egress_pool._check_listener(acc)
    assert acc.status == Status.COOLING

    probe_ok["val"] = True
    await egress_pool.egress_pool._check_listener(acc)
    assert acc.status == Status.ACTIVE
    assert acc.cooling_until is None
    assert acc.id not in egress_pool.egress_pool._listener_cooled
    assert egress_pool.egress_pool._probe_fail_streak.get(acc.id) is None


@pytest.mark.asyncio
async def test_listener_recovery_does_not_touch_foreign_states(listeners_mode, monkeypatch, tmp_path):
    """复活只解除**本循环**的冷却：EXHAUSTED/非本循环冷却等外部状态不被洗掉
    （额度状态机归 quota.py，健康探针不越权）。"""
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test-accounts.db")
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)
    acc = store.add_account("zai", "lis1", "h.eyJzdWIiOiJ1In0.sig")
    acc.enabled = True
    acc.egress = "http://127.0.0.1:11031"

    async def ok_probe(account):
        return True

    monkeypatch.setattr(egress_pool.egress_pool, "health_once", ok_probe)

    # 状态 COOLING 但不是本循环冷却的（gateway 侧风控冷却）→ 探针通不越权复活
    acc.status = Status.COOLING
    acc.cooling_until = time.time() + 900
    await egress_pool.egress_pool._check_listener(acc)
    assert acc.id not in egress_pool.egress_pool._listener_cooled
    assert acc.status == Status.COOLING
    assert acc.cooling_until is not None

    # EXHAUSTED 同理不越权
    acc.status = Status.EXHAUSTED
    await egress_pool.egress_pool._check_listener(acc)
    assert acc.status == Status.EXHAUSTED


@pytest.mark.asyncio
async def test_health_loop_dispatches_by_supply_mode(listeners_mode, monkeypatch, tmp_path):
    """循环按供给模式分派：listeners 下探 listener 出口账号，TP 网关形态
    残留的 egress 不探；tetherpool 账号检查路径不被误调。"""
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test-accounts.db")
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)
    monkeypatch.setattr(settings, "TP_HEALTH_INTERVAL", 0)

    acc = store.add_account("zai", "lis1", "h.eyJzdWIiOiJ1In0.sig")
    acc.enabled = True
    acc.egress = "http://127.0.0.1:11031"
    # ⚠️ token 必须与 lis1 不同：add_account 按 uid 去重，同 token 会返回
    # 同一账号对象（视为换签回写），stale 就成了 acc 自己
    stale = store.add_account("zai", "stale1", "h.eyJzdWIiOiJzIn0.sig")
    stale.enabled = True
    stale.egress = egress.tetherpool_gateway_url(stale)  # 模式残留：不探

    calls = {"listener": 0, "tp": 0}

    async def fake_listener(account):
        calls["listener"] += 1

    async def fake_tp(account):
        calls["tp"] += 1

    monkeypatch.setattr(egress_pool.egress_pool, "_check_listener", fake_listener)
    monkeypatch.setattr(egress_pool.egress_pool, "_check_account", fake_tp)
    # 单例 _loop_task 可能挂在前面测试已关闭的事件循环上（done() 恒 False），
    # 不重置会让 start() 拒绝在当前 loop 上起新任务 → 派发计数恒 0
    egress_pool.egress_pool._loop_task = None

    egress_pool.egress_pool.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        await egress_pool.egress_pool.stop()
    assert calls["listener"] >= 1
    assert calls["tp"] == 0


@pytest.mark.asyncio
async def test_listener_health_respects_external_states(listeners_mode, monkeypatch, tmp_path):
    """2.7.70 根修：探针连败不得洗外部状态——
    ① DISABLED（风控禁用，enabled 仍 True 会被循环纳入）：不覆写、不进 _listener_cooled；
    ② 外部 COOLING（风控指数退避中）：不得被探针冷却缩短；
    ③ INVALID（废 JWT）：同 ①。streak 照常累计供观测。"""
    monkeypatch.setattr(settings, "DB_PATH", tmp_path / "test-accounts.db")
    store = store_mod.Store()
    monkeypatch.setattr(store_mod, "store", store)

    async def fail_probe(account):
        return False

    monkeypatch.setattr(egress_pool.egress_pool, "health_once", fail_probe)

    for status_val in (Status.DISABLED, Status.INVALID):
        acc = store.add_account("zai", f"ext-{status_val}", "h.eyJzdWIiOiJ1In0.sig")
        acc.enabled = True
        acc.egress = "http://127.0.0.1:11031"
        acc.status = status_val
        for _ in range(3):
            await egress_pool.egress_pool._check_listener(acc)
        assert acc.status == status_val, f"{status_val} 不得被洗"
        assert acc.id not in egress_pool.egress_pool._listener_cooled
        assert egress_pool.egress_pool._probe_fail_streak.get(acc.id, 0) >= 2

    # 外部 COOLING（风控退避 1500s）：探针连败不得缩短其冷却
    acc = store.add_account("zai", "ext-cool", "h.eyJzdWIiOiJ1In0.sig")
    acc.enabled = True
    acc.egress = "http://127.0.0.1:11031"
    acc.status = Status.COOLING
    acc.cooling_until = time.time() + 1500
    for _ in range(3):
        await egress_pool.egress_pool._check_listener(acc)
    assert acc.status == Status.COOLING
    assert acc.cooling_until > time.time() + 1400, "外部风控冷却不得被缩短"
    assert acc.id not in egress_pool.egress_pool._listener_cooled

    # 对照：本循环自己冷却的号，探针仍败 → 续冷不受守卫影响
    mine = store.add_account("zai", "mine-cool", "h.eyJzdWIiOiJ1In0.sig")
    mine.enabled = True
    mine.egress = "http://127.0.0.1:11031"
    mine.status = Status.ACTIVE
    egress_pool.egress_pool._listener_cooled.add(mine.id)
    egress_pool.egress_pool._probe_fail_streak[mine.id] = 5
    await egress_pool.egress_pool._check_listener(mine)
    assert mine.status == Status.COOLING
    assert mine.id in egress_pool.egress_pool._listener_cooled
