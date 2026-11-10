"""进程级上游闸门（app/gate.py）单测。

闸门只做三件事，且默认关闭速率/在飞两项（多出口部署由外层网关限速）：
  1. 边缘 405 封 IP → 全进程暂停 plan 渠道 N 秒（默认开启）
  2. 分钟窗口请求预算（GATE_RATE_PER_MIN，0 = 关闭）
  3. 全池在飞上限（GATE_MAX_INFLIGHT，0 = 关闭）

实证出处：2026-11 magpie `opencode-zcode-auth`（同 IP 3 账号 → 405 封地址；
Start Plan ~18 请求/分钟即触发 3008 连发，随后封 IP 1–2 小时）。
"""

from __future__ import annotations

import time

import pytest

from app import settings
from app.gate import Gate


def test_edge_trip_blocks_then_expires(monkeypatch):
    monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 0.25)
    g = Gate()
    assert g.acquire() == (True, "")

    g.trip_edge()
    assert g.edge_hits == 1
    assert g.edge_remaining() > 0
    # 边缘断路器优先于其它维度：即使有在飞余量也必须挡住
    assert g.acquire() == (False, "edge_hold")
    assert g.retry_after() == 1  # ceil(0.25) = 1

    time.sleep(0.3)
    assert g.edge_remaining() == 0
    assert g.acquire() == (True, "")


def test_trip_edge_extends_never_shortens(monkeypatch):
    """连续命中只延长窗口，不会因为后来的短窗口把长窗口截短。"""
    g = Gate()
    monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 10)
    g.trip_edge()
    monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 1)
    g.trip_edge()
    assert g.edge_remaining() > 5
    assert g.edge_hits == 2


def test_edge_hold_zero_disables_breaker(monkeypatch):
    monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 0)
    g = Gate()
    g.trip_edge()
    assert g.edge_remaining() == 0
    assert g.edge_hits == 0  # 未启用不计命中
    assert g.acquire() == (True, "")


def test_rate_window_budget(monkeypatch):
    monkeypatch.setattr(settings, "GATE_RATE_PER_MIN", 2)
    monkeypatch.setattr(settings, "GATE_MAX_INFLIGHT", 0)
    g = Gate()
    assert g.acquire() == (True, "")
    assert g.acquire() == (True, "")
    assert g.acquire() == (False, "rate_limit")
    assert 1 <= g.retry_after() <= 60

    # 窗口滑过后恢复（直接挪窗口起点，免真等 60s）
    g._window_start = time.monotonic() - 61
    assert g.acquire() == (True, "")


def test_rate_budget_counts_each_attempt(monkeypatch):
    """每次上游尝试都占一次预算（重试不是免费的）；释放只还在飞，不还预算。"""
    monkeypatch.setattr(settings, "GATE_RATE_PER_MIN", 1)
    g = Gate()
    assert g.acquire() == (True, "")
    g.release()
    assert g.acquire() == (False, "rate_limit")


def test_inflight_cap(monkeypatch):
    monkeypatch.setattr(settings, "GATE_MAX_INFLIGHT", 1)
    monkeypatch.setattr(settings, "GATE_RATE_PER_MIN", 0)
    g = Gate()
    assert g.acquire() == (True, "")
    assert g.acquire() == (False, "inflight")
    g.release()
    assert g.acquire() == (True, "")


def test_release_never_negative(monkeypatch):
    monkeypatch.setattr(settings, "GATE_MAX_INFLIGHT", 1)
    g = Gate()
    g.release()
    g.release()
    assert g.acquire() == (True, "")


def test_reset_clears_everything(monkeypatch):
    monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 60)
    monkeypatch.setattr(settings, "GATE_RATE_PER_MIN", 1)
    g = Gate()
    g.trip_edge()
    assert g.acquire() == (False, "edge_hold")
    g.reset()
    assert g.edge_hits == 0
    assert g.edge_remaining() == 0
    assert g.acquire() == (True, "")


# ── 忙碌断路器（busy-hold，Magpie BUSY_N/BUSY_HOLD 同款）──────────────────────

def test_busy_hold_trips_at_threshold(monkeypatch):
    monkeypatch.setattr(settings, "GATE_BUSY_THRESHOLD", 3)
    monkeypatch.setattr(settings, "GATE_BUSY_WINDOW_SECONDS", 60)
    monkeypatch.setattr(settings, "GATE_BUSY_HOLD_SECONDS", 120)
    g = Gate()
    g.note_busy()
    g.note_busy()
    assert g.acquire() == (True, "")  # 阈值前不拦
    g.release()
    g.note_busy()
    assert g.acquire() == (False, "busy_hold")  # 第 3 次（60s 窗内）→ 全进程 hold
    assert 1 <= g.retry_after() <= 120


def test_busy_window_slides(monkeypatch):
    monkeypatch.setattr(settings, "GATE_BUSY_THRESHOLD", 2)
    monkeypatch.setattr(settings, "GATE_BUSY_WINDOW_SECONDS", 60)
    monkeypatch.setattr(settings, "GATE_BUSY_HOLD_SECONDS", 10)
    g = Gate()
    g.note_busy()
    g._busy_hits[0] -= 61  # 第一笔滑出窗口
    g.note_busy()
    assert g.acquire() == (True, "")


def test_busy_hold_expires(monkeypatch):
    monkeypatch.setattr(settings, "GATE_BUSY_THRESHOLD", 1)
    monkeypatch.setattr(settings, "GATE_BUSY_HOLD_SECONDS", 0.2)
    g = Gate()
    g.note_busy()
    assert g.acquire() == (False, "busy_hold")
    time.sleep(0.25)
    assert g.acquire() == (True, "")


def test_busy_threshold_zero_disables(monkeypatch):
    monkeypatch.setattr(settings, "GATE_BUSY_THRESHOLD", 0)
    g = Gate()
    for _ in range(10):
        g.note_busy()
    assert g.acquire() == (True, "")


def test_busy_hold_priority_after_edge(monkeypatch):
    """边缘封禁优先于忙碌 hold（封 IP 是更硬的事实）。"""
    monkeypatch.setattr(settings, "GATE_BUSY_THRESHOLD", 1)
    monkeypatch.setattr(settings, "GATE_EDGE_HOLD_SECONDS", 5)
    g = Gate()
    g.note_busy()
    g.trip_edge()
    assert g.acquire() == (False, "edge_hold")


@pytest.mark.parametrize("cap_env", ["GATE_RATE_PER_MIN", "GATE_MAX_INFLIGHT"])
def test_defaults_off(monkeypatch, cap_env):
    """默认不限制：不改变现有部署的吞吐（多出口/多实例由外层限速）。"""
    monkeypatch.setattr(settings, cap_env, 0)
    g = Gate()
    assert g.acquire() == (True, "")
