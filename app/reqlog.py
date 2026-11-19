"""请求监控 —— 网关请求的环形日志（管理端「请求监控」页数据源）。

设计要点：
- 内存 deque 上限 KEEP 条为主读路径（snapshot 零锁竞争外的开销），保证实时观测
- 可选持久化（ZCODE_REQLOG_PERSIST=1）：每条终态追加写 JSONL（write-only，
  无跨进程锁需求，NFS /data 安全），启动时回读尾部 ≤KEEP 条恢复 —— 重启不清零
- 日切：文件侧 _current_day 判据（打开/轮转时更新），启动时与运行中跨天轮转
  reqlog.jsonl.1（保留一代，轮转只发生一次，昨日数据不丢）
- 启动恢复含 .1 尾部：跨日重启后页面仍可见昨日数据（总条数受 KEEP 约束）
- 停机收口：lifespan finally 调 shutdown_flush()，在途请求落盘为 499「网关重启中断」
- 持久化写/开失败打 err 日志并允许下次重试；观测组件不影响网关主链路
- 粒度：一次客户端请求一条；多账号重试不分裂，账号字段记录最终归宿
- ok 三态：None=在途/未知，True=成功，False=失败（含客户端断开 status=499）
- 纯观测：不触碰账号状态与调度逻辑
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

from . import logs, settings

KEEP = 500

_lock = threading.RLock()  # 可重入：finish_* 持锁调用 _persist，其日切分支内再加锁
_entries: deque = deque(maxlen=KEEP)
_inflight: dict[str, dict] = {}
_persist_fh = None  # 持久化追加句柄（惰性打开；None = 未打开，失败可重试）
_current_day = ""  # 当前持久化文件归属日期（YYYY-MM-DD，文件侧日切判据）
_restored = 0  # 本次进程启动时恢复的条数（观测用）


def _today() -> str:
    """服务器本地当日（与页面「今天」筛选的本地零点口径一致）。"""
    return time.strftime("%Y-%m-%d", time.localtime())


def _open_append(target: Path):
    """打开追加句柄并登记归属日期；失败抛 OSError 由调用方处理。

    归属日期取文件首行时间戳（空文件/新建 = 今日）——惰性打开一个
    昨日遗留文件时不得把判据重置为今日，否则运行中日切永远不触发。
    """
    global _current_day
    target.parent.mkdir(parents=True, exist_ok=True)
    fh = target.open("a", encoding="utf-8")
    if target.exists() and target.stat().st_size > 0:
        try:
            with target.open("r", encoding="utf-8") as f:
                first_ts = json.loads(f.readline(4096)).get("ts", 0)
            _current_day = time.strftime("%Y-%m-%d", time.localtime(first_ts))
        except (json.JSONDecodeError, ValueError, OSError):
            _current_day = _today()
    else:
        _current_day = _today()
    return fh


def _fh():
    """惰性打开持久化追加句柄；失败打日志并返回 None（下次可重试）。"""
    global _persist_fh
    if _persist_fh is None:
        try:
            _persist_fh = _open_append(settings.REQLOG_PATH)
        except OSError as err:
            logs.err("reqlog", f"持久化文件打开失败（下次请求重试）: {err}")
            return None
    return _persist_fh


def _rotate_locked(target: Path) -> None:
    """旧日文件轮转 .1（调用方须持锁或不并发；保留一代防膨胀）。"""
    rot = target.with_suffix(target.suffix + ".1")
    rot.unlink(missing_ok=True)
    target.rename(rot)


def _read_tail(target: Path, keep: int, into: deque) -> None:
    """把 target 尾部 ≤keep 条解析进 into（半行撕裂跳过；文件不存在则静默）。"""
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # 半行（断电尾部撕裂）跳过
            if isinstance(e, dict) and "req_id" in e:
                into.append(e)


def restore(path: Path | None = None, keep: int = KEEP) -> int:
    """启动恢复：回读持久化文件尾部 ≤keep 条载入内存（最新在后）。

    仅在持久化开关开启时由 lifespan 调用；返回恢复条数。
    跨日切割：首行时间戳非当日 → 轮转 reqlog.jsonl.1（保留一代）；
    .1 若存在则其尾部一并载入（总条数受 keep 约束），跨日重启后页面仍可见昨日数据。
    """
    global _persist_fh, _restored
    target = path or settings.REQLOG_PATH
    if not settings.REQLOG_PERSIST:
        return 0
    try:
        # 跨日切割：文件首行时间戳非当日 → 轮转，旧日数据进 .1
        if target.exists() and target.stat().st_size > 0:
            with target.open("r", encoding="utf-8") as f:
                first_line = f.readline(4096)
            try:
                first_ts = json.loads(first_line).get("ts", 0)
            except (json.JSONDecodeError, ValueError):
                first_ts = 0
            if first_ts and time.strftime(
                "%Y-%m-%d", time.localtime(first_ts)
            ) != _today():
                _rotate_locked(target)
        loaded: deque = deque(maxlen=keep)
        _read_tail(target.with_suffix(target.suffix + ".1"), keep, loaded)
        _read_tail(target, keep, loaded)
        with _lock:
            _entries.clear()
            _entries.extend(loaded)
        _persist_fh = _open_append(target)
        _restored = len(loaded)
        return len(loaded)
    except OSError as err:
        logs.err("reqlog", f"启动恢复失败（纯内存运行，下次重启重试）: {err}")
        _persist_fh = None
        return 0


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
        _persist(entry)


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
        _persist(entry)


def _persist(entry: dict) -> None:
    """终态条目落盘（调用方须持锁；JSON 一行；文件侧日切；失败静默不影响主链路）。"""
    global _persist_fh
    if not settings.REQLOG_PERSIST:
        return
    fh = _fh()
    if fh is None:
        return
    try:
        # 运行中日切（文件侧判据）：归属日期非当日 → 轮转一次并换新文件
        if _current_day != _today():
            try:
                fh.close()
            except OSError:
                pass
            _rotate_locked(settings.REQLOG_PATH)
            _persist_fh = None
            fh = _fh()
            if fh is None:
                return
        fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
    except OSError as err:
        # 写失败（磁盘满/NFS 抖动）：丢弃本条，句柄作废，下次请求重试打开
        logs.err("reqlog", f"持久化写入失败（丢弃本条，下次重试）: {err}")
        try:
            fh.close()
        except OSError:
            pass
        _persist_fh = None


def snapshot() -> list[dict]:
    """全部条目，最新在前（含在途）。"""
    with _lock:
        return [dict(e) for e in reversed(_entries)]


def shutdown_flush() -> int:
    """优雅停机收口：把仍在途的请求落盘为「中断」终态并关闭句柄。

    在途请求 begin 时不落盘，若不收口，这部分流量在重启后消失。
    收口条目按请求开始日期归属文件（跨日停机时落入前一日文件，语义合理）。
    返回收口条数；由 lifespan finally 调用（持久化关闭时仅清内存）。
    """
    global _persist_fh
    with _lock:
        n = 0
        if settings.REQLOG_PERSIST:
            for entry in _inflight.values():
                entry["ok"] = False
                entry["status"] = 499
                entry["error"] = "网关重启，请求中断"
                entry["t_total"] = time.time() - entry["ts"]
                _persist(entry)
                n += 1
        _inflight.clear()
        _entries.clear()
        if _persist_fh is not None:
            try:
                _persist_fh.close()
            except OSError:
                pass
        _persist_fh = None
        return n


def stats() -> dict:
    """持久化观测状态（管理端监控页展示用）。"""
    with _lock:
        return {
            "persist": bool(settings.REQLOG_PERSIST),
            "file": str(settings.REQLOG_PATH),
            "inflight": len(_inflight),
            "entries": len(_entries),
            "restored": _restored,
        }


def hourly(window_hours: int = 24) -> list[dict]:
    """逐小时聚合（2.7.71 监控增强，管理端监控页/巡检用）。

    每小时桶：请求数、成败、p95/最大总时长、峰值并发（区间重叠扫描，在飞请求
    会计入其起始小时的并发窗口）、模型分布 TOP3。覆盖内存环（KEEP 条）——
    比环更早的小时缺席，不做持久化回扫。按小时升序。"""
    from collections import Counter

    now = time.time()
    intervals: list[tuple[float, float]] = []   # (start, end) 成对区间，逐桶裁剪扫描
    buckets: dict[int, dict] = {}

    def bucket(h: int) -> dict:
        b = buckets.get(h)
        if b is None:
            b = {
                "hour_start": h * 3600,
                "hour": time.strftime("%m-%d %H时", time.localtime(h * 3600)),
                "reqs": 0, "ok": 0, "fail": 0, "inflight_now": 0,
                "p95_t_total": None, "max_t_total": None,
                "peak_concurrency": 0, "models": {},
            }
            buckets[h] = b
        return b

    with _lock:
        for e in _entries:
            ts = e.get("ts") or 0
            if ts <= 0:
                continue
            ok = e.get("ok")
            b = bucket(int(ts // 3600))
            b["reqs"] += 1
            if ok is True:
                b["ok"] += 1
            elif ok is False:
                b["fail"] += 1
            m = e.get("model")
            if m:
                b["models"][m] = b["models"].get(m, 0) + 1
            tt = e.get("t_total")
            if isinstance(tt, (int, float)) and tt >= 0:
                b.setdefault("_tt", []).append(tt)
            dur = max(tt or 0, 0.5)
            if ok is None:
                # 在飞：并发窗口一直开到当前时刻
                intervals.append((ts, now))
                b["inflight_now"] += 1
            else:
                intervals.append((ts, ts + dur))

    for b in buckets.values():
        tts = sorted(b.pop("_tt", []))
        if tts:
            b["p95_t_total"] = round(tts[min(len(tts) - 1, int(len(tts) * 0.95))], 2)
            b["max_t_total"] = round(tts[-1], 2)
        # 并发扫描：与本小时有交集的区间裁剪到 [h0, h1) 后排序扫描
        # （2.7.71 修正：原先 delta 事件流混入跨小时区间导致峰值虚高）
        h0, h1 = b["hour_start"], b["hour_start"] + 3600
        segs = sorted((max(s, h0), min(e, h1)) for s, e in intervals
                      if s < h1 and e > h0)
        ev: list[tuple[float, int]] = []
        for s, e in segs:
            ev.append((s, 1))
            ev.append((e, -1))
        ev.sort(key=lambda x: (x[0], x[1]))
        cur = peak = 0
        for _t, d in ev:
            cur += d
            peak = max(peak, cur)
        b["peak_concurrency"] = peak
        b["models"] = dict(Counter(b["models"]).most_common(3))
    return [buckets[h] for h in sorted(buckets)]


def clear() -> None:
    """清空内存环并删除持久化文件（含 .1 轮转件，管理端「清空」按钮语义）。

    同步重置 _current_day / _restored：截断后文件从今日重新起算，否则跨天
    清空后的首次落盘会对空文件触发轮转，产生空的 .1；恢复计数一并清零，
    避免页面「恢复 N」在清空后仍显示过期统计。
    """
    global _persist_fh, _current_day, _restored
    with _lock:
        _entries.clear()
        _inflight.clear()
        _current_day = _today()
        _restored = 0
        if settings.REQLOG_PERSIST:
            try:
                settings.REQLOG_PATH.write_text("", encoding="utf-8")
                settings.REQLOG_PATH.with_suffix(
                    settings.REQLOG_PATH.suffix + ".1"
                ).unlink(missing_ok=True)
            except OSError:
                pass
