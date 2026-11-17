"""TetherPool 出口管理器（2.7.39 Mode B）——URL 构造 / rebalance 编排 / 互斥。

核心不变式：
- 网关 URL 确定性构造（rebalance 不改 URL，只改 TP 服务端绑定）
- 账号间节点互斥（exclude_node = 其它账号的 egress_node，风控核心）
- rebalance 探针验收（JSON-APP 放行 / HTML-EDGE 换下一个）
- 探针/HTTP 异常安全（PROBE-FAIL ≠ 边缘封禁，前者换节点后者隔离）
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app import egress, egress_pool, settings
from app import store as store_mod
from app.egress_pool import gateway_url_for
from app.models import Account

# autouse fixture 会把 probe_alicdn 换成恒过假探针；probe_alicdn 本体的
# 单测需要真实现——在 import 期（fixture 生效前）捕获原函数引用。
_REAL_PROBE_ALICDN = egress_pool.probe_alicdn


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
async def test_rebalance_recovers_after_all_rounds_fail(tp_mode, monkeypatch):
    """rebalance 3 轮全败：服务端绑定已随 force_rebalance 移走——第 4 轮
    acquire+探针验收，hub 记录必须对齐 TP 实际绑定（不留在旧节点）。"""
    seq = {"probe": 0}

    async def fake_tp_ban(node, reason, evidence, account_name):
        pass

    async def fake_tp_acquire(account, force_rebalance=False, extra_exclude=None):
        return {"node_name": f"node-cand-{seq['probe']}"}

    async def fake_probe(account, proxy_url, jwt):
        seq["probe"] += 1
        return 0, "ConnectError: dead"  # 前三轮全部探不通

    monkeypatch.setattr(egress_pool, "tp_ban", fake_tp_ban)
    monkeypatch.setattr(egress_pool, "tp_acquire", fake_tp_acquire)
    monkeypatch.setattr(egress_pool.egress_pool, "_probe_via", fake_probe)

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
