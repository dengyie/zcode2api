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


@pytest.mark.asyncio
async def test_transport_abort_emits_fail_frames(tp_mode, monkeypatch):
    """迭代二 P1 回归防线（2026-11-22 迭代三收口）：流已开帧后上游传输层断开
    （ReadTimeout/Server disconnected 等普通 Exception，非 UpstreamStreamError）
    时，带转译层的路径必须产出客户端可识别失败帧——此前半截回答以干净 EOF
    呈现成功；且 fail_frames 在普通异常上不得炸 AttributeError（收口前
    `err.code` 直接 AttributeError，修复在最常见触发形态下自身崩溃）。"""
    acc = _account()
    up = type("Up", (), {"account": acc, "t_first": None,
                         "close": lambda *a: asyncio.sleep(0)})()
    transport_err = TimeoutError("timed out")  # httpx.ReadTimeout 同形态

    def fail_frames(err):
        # responses 路径同款契约：读 code 属性（曾因普通异常无此属性而炸）
        code = getattr(err, "code", None)
        return [f"event: response.failed\ndata: {code or 'upstream_error'}\n\n"]

    @gw._sse_guard("req-ttf", up, fail_frames=fail_frames)
    async def guarded():
        yield "event: response.created\n\n"
        raise transport_err

    frames = [f async for f in guarded()]
    # 起始帧 + 传输中断失败帧
    assert frames == ["event: response.created\n\n",
                      "event: response.failed\ndata: upstream_error\n\n"], \
        f"传输中断未产出失败帧或误炸: {frames}"


@pytest.mark.asyncio
async def test_transport_abort_openai_fail_frames_no_attrerror(tp_mode, monkeypatch):
    """openai-compat 路径传输中断：error_sse(lambda 读 err.code)在普通
    Exception 上曾 AttributeError——归一后须正常产出错误帧。"""
    from app.openai_compat import StreamConverter

    acc = _account()
    up = type("Up", (), {"account": acc, "t_first": None,
                         "close": lambda *a: asyncio.sleep(0)})()
    conv = StreamConverter("claude-x")
    req_id = "req-oai"

    @gw._sse_guard(req_id, up, fail_frames=lambda err: (
        gw.error_sse(str(err), getattr(err, "code", None)), conv.done()))
    async def guarded():
        yield conv.start()
        raise TimeoutError("read timeout")

    frames = [f async for f in guarded()]
    assert any("upstream_stream_error" in f for f in frames), \
        f"openai 路径传输中断未产出 error_sse: {frames}"


@pytest.mark.asyncio
async def test_openai_stream_without_message_delta_emits_error_frame(tp_mode):
    """上游 SSE 流无 message_delta 即结束时，不得静默发 [DONE] 充当成功。"""
    acc = _account()

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            yield 'data: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":3}}}'

    up = type("Up", (), {
        "account": acc,
        "resp": _FakeResp(),
        "t_first": 0.1,
        "close": lambda *a: asyncio.sleep(0),
    })()

    resp = gw._openai_stream_response(up, "GLM-5.3-Flash", "req-trunc")
    chunks = [c async for c in resp.body_iterator]
    joined = "".join(chunks)
    assert "upstream_stream_truncated" in joined
    assert joined.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_to_streaming_empty_bytes_triggers_abort(tp_mode):
    """上游返回 0 字节空流时，to_streaming 必须触发流中断记账。"""
    acc = _account()

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self):
            if False:
                yield b""

    up = gw._Upstream(_FakeResp(), None, None, t_first=0.1, account=acc)
    resp = up.to_streaming("req-empty")
    chunks = [c async for c in resp.body_iterator]
    assert chunks == []
    assert acc.recent_results and acc.recent_results[-1]["ok"] is False
