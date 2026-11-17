"""运维事件日志 — ANSI 彩色 stdout + 结构化 JSONL 落盘（opslog）双通道。

stdout 彩色行 = 人眼实时观测（supervisor out.log，版式 2026-11-14 边缘 405
复盘定型）；opslog JSONL = 机器可查的完整事件流（时间戳含年、级别/模块
字段化），跨日排查不再受 out.log 10MB 轮转截断影响。

落盘设计对齐 reqlog.py 成熟模式（write-only 追加、文件侧日切、.1 轮转
保留一代、写失败降级不影响主链路、NFS /data 安全）。开关
ZCODE_OPSLOG_PERSIST（默认开）；关闭时零开销（一个布尔短路）。

排障口径（2.7.58-61 领取失败潮实战提炼）：
- grep 时间线：opslog 每行独立 JSON，`_ts` 含年份，跨日事件可直接
  按日期切片，不必再从 ANSI 脏文本里抠 MM-DD；
- grep 模块：`tag` 字段与 stdout 的模块标签逐字一致（claim/captcha/
  egress_pool/gateway...），`jq -r 'select(.tag=="captcha")'` 一刀切；
- grep 级别：level ∈ ok/info/warn/err，与 [+] [-] [~] [!] 一一对应。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

_R = "\033[0m"
_G = "\033[32m"
_C = "\033[36m"
_Y = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[90m"
_B = "\033[1m"
_MAG = "\033[35m"
_W = "\033[37m"

# opslog 落盘句柄（logs.py 自持，避免与 reqlog 的日切/句柄逻辑耦合）
_fh = None
_current_day = ""
_lock = threading.Lock()
_enabled: bool | None = None  # 惰性读 settings（logs 被 settings 前的模块引用）
_path: Path | None = None

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _stamp() -> str:
    return f"{_DIM}[{time.strftime('%m-%d %H:%M:%S')}]{_R}"


def _opslog(level: str, tag: str, msg: str) -> None:
    """事件行落盘 opslog.jsonl（write-only 追加；日切 + .1 轮转保留一代）。

    任何失败都静默吞掉（磁盘满/NFS 抖动 → 丢弃本条、句柄作废、下条重试）：
    观测组件绝不影响主链路。stdout print 本身已是第一通道，这里只做增强。
    """
    global _fh, _current_day, _enabled, _path
    if _enabled is None:
        try:
            from . import settings

            _enabled = bool(settings.OPSLOG_PERSIST)
            _path = settings.OPSLOG_PATH
        except Exception:  # pragma: no cover - settings 尚未就绪的极端时序
            _enabled = False
    if not _enabled or _path is None:
        return
    entry = {
        "_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "tag": tag,
        "msg": _ANSI_RE.sub("", msg),
    }
    try:
        with _lock:
            if _fh is None:
                _path.parent.mkdir(parents=True, exist_ok=True)
                _fh = _path.open("a", encoding="utf-8")
                if _path.exists() and _path.stat().st_size > 0:
                    with _path.open("r", encoding="utf-8") as f:
                        first_ts = (json.loads(f.readline(4096) or "{}")).get("_ts", "")
                    _current_day = first_ts[:10] or time.strftime("%Y-%m-%d")
                else:
                    _current_day = time.strftime("%Y-%m-%d")
            today = time.strftime("%Y-%m-%d")
            if _current_day != today:
                _fh.close()
                rot = _path.with_suffix(_path.suffix + ".1")
                rot.unlink(missing_ok=True)
                _path.rename(rot)
                _fh = _path.open("a", encoding="utf-8")
                _current_day = today
            _fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            _fh.flush()
    except (OSError, ValueError):
        try:
            if _fh is not None:
                _fh.close()
        except OSError:
            pass
        _fh = None


def _emit(level: str, marker: str, color: str, tag: str, msg: str, plain: bool = False):
    """stdout 彩色行（人读）+ opslog 落盘（机器查）。plain=True 不落盘。"""
    print(f"  {_stamp()} {color}{marker}{_R} {_DIM}{tag}{_R} {msg}")
    if not plain:
        _opslog(level, tag, msg)


def ok(module: str, msg: str):
    _emit("ok", "[+]", _G, module, msg)


def info(module: str, msg: str):
    _emit("info", "[-]", _DIM, module, msg)


def warn(module: str, msg: str):
    _emit("warn", "[~]", _Y, module, msg)


def err(module: str, msg: str):
    _emit("err", "[!]", _RED, module, msg)


def req(req_id: str, model: str, stream: bool, last_msg: str):
    """请求日志 — 一行显示关键信息"""
    s = "stream" if stream else "sync"
    msg_preview = last_msg[:40] + ("..." if len(last_msg) > 40 else "")
    print(f"  {_stamp()} {_C}>>>{_R} {_DIM}{req_id}{_R}  {_W}{model}{_R}  {_DIM}{s}{_R}  {_DIM}\"{msg_preview}\"{_R}")
    _opslog("req", "gateway", f">>> {req_id} {model} {s} \"{msg_preview}\"")


def req_ok(req_id: str, tokens: int = 0):
    """请求完成"""
    t = f"  {_DIM}{tokens}tok{_R}" if tokens else ""
    print(f"  {_stamp()} {_G}<<<{_R} {_DIM}{req_id}{_R}{t}")
    _opslog("req_ok", "gateway", f"<<< {req_id}" + (f" {tokens}tok" if tokens else ""))


def req_err(req_id: str, msg: str):
    """请求错误 — 只显示 req_id + 简短原因，不泄露上游"""
    print(f"  {_stamp()} {_RED}<!>{_R} {_DIM}{req_id}{_R}  {msg}")
    _opslog("req_err", "gateway", f"<!> {req_id}  {msg}")


def banner(lines: list[str]):
    """启动横幅 — 保持无时间戳（版式块，非事件行）"""
    w = max(len(_ANSI_RE.sub("", line)) for line in lines) + 4
    border = f"{_DIM}{'=' * w}{_R}"
    print(f"\n{border}")
    for line in lines:
        print(f"  {line}")
    print(f"{border}\n")


def opslog_stats() -> dict:
    """opslog 落盘状态（管理端/健康页可展示；纯观测）。"""
    return {
        "persist": bool(_enabled),
        "file": str(_path) if _path else "",
        "day": _current_day,
        "open": _fh is not None,
    }


def opslog_close() -> None:
    """停机收口：关闭落盘句柄（lifespan finally 调用；防止缓冲丢失）。"""
    global _fh
    with _lock:
        if _fh is not None:
            try:
                _fh.close()
            except OSError:
                pass
            _fh = None
