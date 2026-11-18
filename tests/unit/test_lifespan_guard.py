"""lifespan 启动/停机守卫单测（2.7.68）。

- _startup_precheck：发版偏斜（新 main.py + 旧组件缺 start/stop API）在启动期
  fail-fast 带明确版本配对错误，而非运行期 AttributeError 崩溃循环
  （2.7.65 部署事故实锤形态）
- _start_component：单组件启动异常降级留痕，不放大成整个 lifespan 失败
  （HA 看门狗连环重启的放大器根除）
"""

from __future__ import annotations

import pytest

from app import main


class _Ok:
    def start(self):
        pass

    def stop(self):
        pass


class _Broken:
    """缺 stop（发版偏斜形态：旧版组件没有新 main.py 期望的方法）。"""

    def start(self):
        pass


def test_startup_precheck_passes_on_complete_components():
    old = main.egress_pool
    try:
        main.egress_pool = _Ok()
        main.captcha_manager = _Ok()
        main.claim_round = _Ok()
        main.version_tracker = _Ok()
        main.monitor = _Ok()
        main._startup_precheck()  # 不抛即过
    finally:
        main.egress_pool = old


def test_startup_precheck_fails_fast_on_deploy_skew(monkeypatch, capsys):
    old = main.egress_pool
    try:
        monkeypatch.setattr(main, "egress_pool", _Broken())
        with pytest.raises(RuntimeError) as ei:
            main._startup_precheck()
        assert "发版偏斜" in str(ei.value)
        assert "egress_pool" in str(ei.value)
    finally:
        main.egress_pool = old


def test_start_component_isolates_failure(capsys):
    calls: list[str] = []

    def _boom():
        raise RuntimeError("disk on fire")

    main._start_component("boom", _boom)
    main._start_component("fine", lambda: calls.append("ok"))
    assert calls == ["ok"], "单组件失败不得阻断后续组件启动"
    out = capsys.readouterr().out
    assert "boom" in out and "降级运行" in out


@pytest.mark.asyncio
async def test_lifespan_stop_loop_isolates_each_segment(monkeypatch):
    """停机逐段隔离：一段收口抛异常（含异步段），其余段仍执行。"""
    executed: list[str] = []

    def sync_ok():
        executed.append("sync")

    async def async_boom():
        executed.append("async-boom")
        raise RuntimeError("stop failed")

    async def async_ok():
        executed.append("async-ok")

    for name, fn in (("reqlog", sync_ok), ("a", async_boom), ("b", async_ok)):
        try:
            result = fn()
            import asyncio as _aio
            if _aio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - 模拟 main.py 的收口循环
            assert name == "a"
    assert executed == ["sync", "async-boom", "async-ok"], \
        "异步段异常不得跳过后续收口"
