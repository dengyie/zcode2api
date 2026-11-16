"""流中途中断的账号记账与换绑触发（2026-11-16 trial-mvxmwyrg 事故回归）。

事故链（两个独立缺陷叠加）：
1. 流中断盲区：坏节点让真实请求在建连后、读到一半时断（RemoteProtocolError /
   incomplete chunked read），该异常走 _sse_guard「流传输中断」与非流式 aread
   的 except——两条路径此前只写 reqlog：不进 recent_results、不 note_conn_fail、
   不触发 egress_pool。
2. import 错位（自 aa960eb/2.7.39 起）：`from .. import egress_pool` 拿到的是
   模块而非单例实例，`egress_pool.verify_then_rebalance(...)` 每执行必
   AttributeError（_spawn_bg 后台任务吞掉、无日志）——建连失败触发器也从未
   真正生效。修复后统一 `from ..egress_pool import egress_pool` 单例引用。
"""

from __future__ import annotations

import asyncio

import pytest

from app import egress, settings
from app.egress_pool import egress_pool as singleton
from app.models import Account
from app.routes import gateway as gw


@pytest.fixture
def tp_mode(monkeypatch):
    """与 test_egress_pool 同款 fixture：打开 per-account TP 供给。"""
    monkeypatch.setattr(settings, "PER_ACCOUNT_EGRESS", True)
    monkeypatch.setattr(settings, "EGRESS_SUPPLY", "tetherpool")
    monkeypatch.setattr(settings, "TP_GATEWAY", "127.0.0.1:9101")
    monkeypatch.setattr(settings, "TP_TYPE", "clash_proxy")
    monkeypatch.setattr(settings, "TP_REGION", "us-west")
    monkeypatch.setattr(settings, "TP_TIER", "standard")
    monkeypatch.setattr(settings, "TP_SERVICE", "zcode-hub")
    monkeypatch.setattr(settings, "TP_REBALANCE_DEBOUNCE", 0)


def _account(acc_id="acc-stream", name="sa", node="half-dead-node"):
    return Account(id=acc_id, name=name, provider="zai", mode="jwt",
                   jwt_token="h.eyJzdWIiOiJ1In0.sig",
                   egress_node=node, egress="http://gw:9101")


def test_gateway_uses_singleton_not_module():
    """import 错位回归：gateway 必须持有单例实例（模块上没有触发器方法）。"""
    assert gw._egress_pool_singleton is singleton
    assert hasattr(singleton, "verify_then_rebalance")


@pytest.mark.asyncio
async def test_stream_abort_records_result_and_triggers_rebalance(tp_mode, monkeypatch):
    acc = _account()
    up = type("Up", (), {"account": acc})()
    err = RuntimeError("peer closed connection without sending complete message body")

    rebalanced = {"n": 0}

    async def fake_rebalance_locked(account, reason, ban=True):
        rebalanced["n"] += 1

    async def fake_health_once(account):
        return True  # 探针通（半死节点指纹）——单次不换，聚类达阈值必换

    monkeypatch.setattr(singleton, "_rebalance_locked", fake_rebalance_locked)
    monkeypatch.setattr(singleton, "health_once", fake_health_once)

    tag = egress.messages_egress_tag(acc)
    egress.clear_conn_fails(tag)
    try:
        # 单次流中断：记结果 + 记聚类，但未达阈值不换绑（防抖语义不变）
        gw._note_stream_abort(up, err, "req1")
        assert any(not e["ok"] and "流中断" in e["detail"]
                   for e in acc.recent_results), "失败必须进 recent_results"
        # 等 _spawn_bg 的 verify 任务跑完（探针通 → 不换）
        for _ in range(20):
            await asyncio.sleep(0.01)
            if rebalanced["n"]:
                break
        assert rebalanced["n"] == 0

        # 累积到聚类阈值：无论探针结果强制换绑
        for _ in range(egress.CONN_FAIL_THRESHOLD):
            egress.note_conn_fail(tag)
        gw._note_stream_abort(up, err, "req2")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if rebalanced["n"]:
                break
        assert rebalanced["n"] == 1, "聚类达阈值后流中断必须触发换绑"
        assert not egress.conn_fail_clustered(tag), "触发后聚类窗口应清零"
    finally:
        egress.clear_conn_fails(tag)


@pytest.mark.asyncio
async def test_stream_abort_without_account_is_noop(tp_mode):
    up = type("Up", (), {"account": None})()
    gw._note_stream_abort(up, RuntimeError("x"), "req9")  # 不抛即通过
