"""请求监控 —— 网关请求的内存环形日志（管理端「请求监控」页数据源）。

设计要点：
- 仅内存（deque 上限 500 条），重启即清零：定位是运维实时观测而非审计，
  避免每请求写库的 IO 放大（与 billing 刷新去抖同一节流思路）
- 粒度：一次客户端请求一条；多账号重试不分裂，账号字段记录最终归宿
- ok 三态：None=在途/未知，True=成功，False=失败（含客户端断开 status=499）
- 纯观测：不触碰账号状态与调度逻辑
"""

from __future__ import annotations

import threading
import time
from collections import deque

KEEP = 500

_lock = threading.Lock()
_entries: deque = deque(maxlen=KEEP)
_inflight: dict[str, dict] = {}


def begin(req_id: str, endpoint: str, model: str, stream: bool, preview: str = "") -> None:
    """请求进入网关（鉴权通过、body 解析成功后）。"""
    entry = {
        "req_id": req_id,
        "ts": time.time(),
        "endpoint": endpoint,
        "model": model,
        "stream": bool(stream),
        "preview": (preview or "")[:80],
        "account": "",
        "mode": "",
        "ok": None,
        "status": None,
        "error": "",
        "t_first": None,
        "t_total": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    with _lock:
        _entries.append(entry)
        _inflight[req_id] = entry


def mark_account(req_id: str, account_name: str, mode: str) -> None:
    """记录实际服务该请求的账号（多账号重试时最后一次生效）。"""
    with _lock:
        entry = _inflight.get(req_id)
        if entry is not None:
            entry["account"] = account_name
            entry["mode"] = mode


def finish_ok(req_id: str, t_first: float | None = None,
              input_tokens: int | None = None, output_tokens: int | None = None,
              status: int | None = None) -> None:
    with _lock:
        entry = _inflight.pop(req_id, None)
        if entry is None:
            return
        entry["ok"] = True
        entry["status"] = status or 200
        entry["t_first"] = t_first
        entry["t_total"] = time.time() - entry["ts"]
        entry["input_tokens"] = input_tokens
        entry["output_tokens"] = output_tokens


def finish_error(req_id: str, error: str, status: int | None = None,
                 t_first: float | None = None) -> None:
    with _lock:
        entry = _inflight.pop(req_id, None)
        if entry is None:
            return
        entry["ok"] = False
        entry["status"] = status
        entry["error"] = (error or "")[:200]
        entry["t_first"] = t_first
        entry["t_total"] = time.time() - entry["ts"]


def snapshot() -> list[dict]:
    """全部条目，最新在前（含在途）。"""
    with _lock:
        return [dict(e) for e in reversed(_entries)]


def clear() -> None:
    with _lock:
        _entries.clear()
        _inflight.clear()
