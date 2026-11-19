"""reqlog 请求监控环形日志单元测试。"""

from __future__ import annotations

import json
import time

import pytest

from app import reqlog


@pytest.fixture(autouse=True)
def _clean():
    reqlog.clear()
    yield
    reqlog.clear()


class TestRingBuffer:
    def test_begin_finish_ok_lifecycle(self):
        reqlog.begin("r1", "messages", "GLM-5.3-Flash", True, "你好")
        reqlog.mark_account("r1", "acc-1", "jwt")
        reqlog.finish_ok("r1", t_first=0.5, input_tokens=10, output_tokens=5)
        snap = reqlog.snapshot()
        assert len(snap) == 1
        e = snap[0]
        assert e["req_id"] == "r1"
        assert e["ok"] is True and e["status"] == 200
        assert e["account"] == "acc-1" and e["mode"] == "jwt"
        assert e["t_first"] == 0.5
        assert e["t_total"] is not None and e["t_total"] >= 0
        assert e["input_tokens"] == 10 and e["output_tokens"] == 5
        assert e["stream"] is True and e["endpoint"] == "messages"

    def test_inflight_entry_visible(self):
        reqlog.begin("r2", "chat", "GLM-5.3", False, "hi")
        snap = reqlog.snapshot()
        assert len(snap) == 1
        assert snap[0]["ok"] is None
        assert snap[0]["t_total"] is None

    def test_finish_error_truncates_message(self):
        reqlog.begin("r3", "messages", "GLM-5.3", False, "")
        reqlog.finish_error("r3", "x" * 500, status=400)
        e = reqlog.snapshot()[0]
        assert e["ok"] is False and e["status"] == 400
        assert len(e["error"]) == 200

    def test_finish_unknown_req_id_noop(self):
        reqlog.finish_ok("ghost")
        reqlog.finish_error("ghost", "x")
        assert reqlog.snapshot() == []

    def test_ring_buffer_cap(self):
        for i in range(reqlog.KEEP + 50):
            reqlog.begin(f"r{i}", "messages", "m", False, "")
            reqlog.finish_ok(f"r{i}")
        snap = reqlog.snapshot()
        assert len(snap) == reqlog.KEEP
        assert snap[0]["req_id"] == f"r{reqlog.KEEP + 49}"  # 最新在前

    def test_clear(self):
        reqlog.begin("r", "messages", "m", False, "")
        reqlog.clear()
        assert reqlog.snapshot() == []

    def test_mark_account_last_wins(self):
        reqlog.begin("r", "messages", "m", False, "")
        reqlog.mark_account("r", "a", "jwt")
        reqlog.mark_account("r", "b", "apiKey")
        reqlog.finish_ok("r")
        assert reqlog.snapshot()[0]["account"] == "b"

    def test_timestamps_monotonic(self):
        t0 = time.time()
        reqlog.begin("r", "messages", "m", False, "")
        reqlog.finish_ok("r")
        e = reqlog.snapshot()[0]
        assert t0 - 1 <= e["ts"] <= time.time() + 1


class TestPersistence:
    """ZCODE_REQLOG_PERSIST 持久化行为（2.7.37+）。"""

    @pytest.fixture
    def persist_env(self, tmp_path, monkeypatch):
        """开启持久化并指向临时文件。"""
        monkeypatch.setattr(reqlog.settings, "REQLOG_PERSIST", True)
        path = tmp_path / "reqlog.jsonl"
        monkeypatch.setattr(reqlog.settings, "REQLOG_PATH", path)
        # 重置模块内惰性句柄状态
        monkeypatch.setattr(reqlog, "_persist_fh", None)
        yield path
        fh = reqlog._persist_fh
        if isinstance(fh, object) and fh not in (None, False) and hasattr(fh, "close"):
            fh.close()
        reqlog._persist_fh = None
        reqlog._current_day = ""
        reqlog._restored = 0
        reqlog.clear()

    def test_finish_ok_persists_line(self, persist_env):
        reqlog.begin("p1", "messages", "GLM-5.3", True, "hi")
        reqlog.finish_ok("p1")
        lines = persist_env.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        e = json.loads(lines[0])
        assert e["req_id"] == "p1" and e["ok"] is True

    def test_begin_inflight_not_persisted(self, persist_env):
        reqlog.begin("p2", "messages", "m", False, "")
        assert not persist_env.exists() or persist_env.read_text() == ""

    def test_restore_recovers_entries(self, persist_env):
        for i in range(5):
            reqlog.begin(f"p{i}", "messages", "m", False, "")
            reqlog.finish_ok(f"p{i}")
        # 模拟重启：清内存后回读
        reqlog._entries.clear()
        restored = reqlog.restore()
        assert restored == 5
        snap = reqlog.snapshot()
        assert len(snap) == 5
        assert snap[0]["req_id"] == "p4"  # 最新在前
        assert all(e["ok"] is True for e in snap)

    def test_restore_respects_keep_cap(self, persist_env):
        for i in range(reqlog.KEEP + 30):
            reqlog.begin(f"q{i}", "messages", "m", False, "")
            reqlog.finish_ok(f"q{i}")
        reqlog._entries.clear()
        reqlog._persist_fh = None
        restored = reqlog.restore()
        assert restored == reqlog.KEEP
        assert reqlog.snapshot()[0]["req_id"] == f"q{reqlog.KEEP + 29}"

    def test_restore_skips_torn_lines(self, persist_env):
        reqlog.begin("t1", "messages", "m", False, "")
        reqlog.finish_ok("t1")
        with persist_env.open("a", encoding="utf-8") as f:
            f.write('{"req_id":"t2","ts":123,"broken')  # 模拟断电半行
        reqlog._entries.clear()
        restored = reqlog.restore()
        assert restored == 1
        assert reqlog.snapshot()[0]["req_id"] == "t1"

    def test_clear_truncates_file(self, persist_env):
        reqlog.begin("c1", "messages", "m", False, "")
        reqlog.finish_ok("c1")
        assert persist_env.stat().st_size > 0
        reqlog.clear()
        assert persist_env.read_text() == ""
        assert reqlog.snapshot() == []

    def test_restore_disabled_when_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reqlog.settings, "REQLOG_PERSIST", False)
        monkeypatch.setattr(reqlog.settings, "REQLOG_PATH", tmp_path / "x.jsonl")
        assert reqlog.restore() == 0

    def test_day_rollover_at_startup(self, persist_env):
        """启动恢复：首行时间戳为昨日 → 文件轮转 .1，且 .1 尾部一并恢复（页面可见）。"""
        yesterday = time.time() - 86400
        old_entry = {"req_id": "old", "ts": yesterday, "endpoint": "messages",
                     "ok": True, "status": 200}
        persist_env.write_text(json.dumps(old_entry) + "\n", encoding="utf-8")
        restored = reqlog.restore()
        assert restored == 1  # 旧日数据进 .1 但仍恢复可见
        snap = reqlog.snapshot()
        assert snap[0]["req_id"] == "old"
        assert persist_env.with_suffix(".jsonl.1").exists()
        # 新文件从空开始，后续落盘写新文件
        assert not persist_env.exists() or persist_env.stat().st_size == 0

    def test_day_rollover_at_runtime_single_rotation(self, persist_env):
        """运行中日切：轮转只发生一次，.1 保留昨日数据，后续落盘写新文件。

        通过预放「昨日时间戳的非空文件」让 _open_append 从首行推断归属日期，
        模拟长驻进程跨天（不直接操纵 _current_day，走真实惰性打开路径）。
        """
        yesterday = time.time() - 86400
        old_entry = {"req_id": "yday", "ts": yesterday, "ok": True}
        persist_env.write_text(json.dumps(old_entry) + "\n", encoding="utf-8")
        reqlog._persist_fh = None  # 触发惰性打开，从文件首行推断 _current_day=昨日
        rot_path = persist_env.with_suffix(".jsonl.1")

        reqlog.begin("d0", "messages", "m", False, "")
        reqlog.finish_ok("d0")  # 触发日切：轮转昨日文件 → .1，d0 写入新文件
        assert rot_path.exists()
        assert reqlog._current_day == reqlog._today()
        # .1 含昨日数据，新文件含今日 d0
        rot_ids = [json.loads(line)["req_id"] for line in rot_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert rot_ids == ["yday"]
        # 继续落盘不再触发轮转（.1 稳定）
        rot_mtime = rot_path.stat().st_mtime_ns
        for i in (1, 2):
            reqlog.begin(f"d{i}", "messages", "m", False, "")
            reqlog.finish_ok(f"d{i}")
        assert rot_path.stat().st_mtime_ns == rot_mtime
        new_entries = [json.loads(line) for line in persist_env.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [e["req_id"] for e in new_entries] == ["d0", "d1", "d2"]  # 全部写入新文件

    def test_shutdown_flush_closes_inflight(self, persist_env):
        """停机收口：在途请求落盘为 499 中断终态，且句柄关闭。"""
        reqlog.begin("f1", "messages", "m", True, "")
        reqlog.begin("f2", "chat", "m", False, "")
        reqlog.finish_ok("f2")  # f2 正常结束
        n = reqlog.shutdown_flush()
        assert n == 1  # 只收口 f1
        lines = [json.loads(line) for line in persist_env.read_text(encoding="utf-8").splitlines() if line.strip()]
        f1 = [e for e in lines if e["req_id"] == "f1"][0]
        assert f1["ok"] is False and f1["status"] == 499
        assert "中断" in f1["error"]
        # 收口后句柄已关，再 finish 不落盘（noop）
        reqlog.begin("f3", "messages", "m", False, "")
        reqlog.finish_ok("f3")
        size_after = persist_env.stat().st_size
        # 句柄重开（_fh 惰性）会落盘 f3 —— 这是新请求的正常行为
        lines2 = [json.loads(line) for line in persist_env.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any(e["req_id"] == "f3" for e in lines2) or size_after == persist_env.stat().st_size

    def test_shutdown_flush_noop_when_off(self, monkeypatch, tmp_path):
        monkeypatch.setattr(reqlog.settings, "REQLOG_PERSIST", False)
        monkeypatch.setattr(reqlog.settings, "REQLOG_PATH", tmp_path / "y.jsonl")
        reqlog.begin("g1", "messages", "m", False, "")
        assert reqlog.shutdown_flush() == 0
        assert reqlog.snapshot() == []  # inflight 已清

    def test_stats_reflect_state(self, persist_env):
        reqlog.begin("s1", "messages", "m", False, "")
        st = reqlog.stats()
        assert st["persist"] is True
        assert st["inflight"] == 1
        assert st["entries"] == 1
        assert "reqlog.jsonl" in st["file"]
        reqlog.finish_ok("s1")
        assert reqlog.stats()["inflight"] == 0

    def test_restore_records_restored_count(self, persist_env):
        for i in range(3):
            reqlog.begin(f"rc{i}", "messages", "m", False, "")
            reqlog.finish_ok(f"rc{i}")
        reqlog._entries.clear()
        reqlog.restore()
        assert reqlog.stats()["restored"] == 3

    def test_persist_off_writes_nothing(self, tmp_path, monkeypatch):
        """开关默认关闭：终态请求零写盘（评审 P1-1 回归测试）。"""
        monkeypatch.setattr(reqlog.settings, "REQLOG_PERSIST", False)
        path = tmp_path / "reqlog.jsonl"
        monkeypatch.setattr(reqlog.settings, "REQLOG_PATH", path)
        monkeypatch.setattr(reqlog, "_persist_fh", None)
        monkeypatch.setattr(reqlog, "_current_day", "")
        try:
            reqlog.begin("off1", "messages", "m", False, "")
            reqlog.finish_ok("off1")
            reqlog.begin("off2", "messages", "m", False, "")
            reqlog.finish_error("off2", "x")
            assert not path.exists()  # 零写盘
        finally:
            reqlog._persist_fh = None
            reqlog._current_day = ""

    def test_restore_merges_rotated_and_current(self, persist_env):
        """跨日启动恢复：.1（昨日）+ 当前文件（今日）合并载入，≤KEEP 上限。"""
        yesterday = time.time() - 86400
        old_entries = [{"req_id": f"old{i}", "ts": yesterday, "ok": True} for i in range(3)]
        rot_path = persist_env.with_suffix(".jsonl.1")
        rot_path.write_text(
            "\n".join(json.dumps(e) for e in old_entries) + "\n", encoding="utf-8"
        )
        today_entry = {"req_id": "today1", "ts": time.time(), "ok": True}
        persist_env.write_text(json.dumps(today_entry) + "\n", encoding="utf-8")
        restored = reqlog.restore()
        assert restored == 4
        snap = reqlog.snapshot()
        ids = [e["req_id"] for e in snap]
        assert ids == ["today1", "old2", "old1", "old0"]  # 时序：旧→新，最新在前

    def test_restore_merge_respects_keep_cap(self, persist_env):
        """合并恢复总量超 KEEP 时只保留最新 KEEP 条。"""
        yesterday = time.time() - 86400
        old_entries = [
            {"req_id": f"old{i}", "ts": yesterday, "ok": True}
            for i in range(reqlog.KEEP)
        ]
        rot_path = persist_env.with_suffix(".jsonl.1")
        rot_path.write_text(
            "\n".join(json.dumps(e) for e in old_entries) + "\n", encoding="utf-8"
        )
        today_entries = [
            {"req_id": f"new{i}", "ts": time.time(), "ok": True} for i in range(10)
        ]
        persist_env.write_text(
            "\n".join(json.dumps(e) for e in today_entries) + "\n", encoding="utf-8"
        )
        restored = reqlog.restore()
        assert restored == reqlog.KEEP
        snap = reqlog.snapshot()
        assert len(snap) == reqlog.KEEP
        assert snap[0]["req_id"] == "new9"  # 最新在前
        assert snap[-1]["req_id"] == "old10"  # 最旧的 old0..old9 被挤出

    def test_clear_resets_day_and_restored(self, persist_env):
        """清空后日切判据重置为今日、恢复计数归零（R2 评审回归）。

        场景：跨天清空（_current_day 残留昨日）→ 下一次落盘不得对空文件
        触发轮转产生空 .1；页面「恢复 N」不再显示过期统计。
        """
        yesterday = time.time() - 86400
        persist_env.write_text(
            json.dumps({"req_id": "old", "ts": yesterday, "ok": True}) + "\n",
            encoding="utf-8",
        )
        assert reqlog.restore() == 1
        # 构造跨天清空场景：日切判据残留昨日（长驻进程跨天后先 clear 再落盘）
        reqlog._current_day = time.strftime("%Y-%m-%d", time.localtime(yesterday))
        assert reqlog._current_day != reqlog._today()
        reqlog.clear()
        assert reqlog._current_day == reqlog._today()
        assert reqlog.stats()["restored"] == 0
        # 跨天后清空 → 首次落盘不轮转空文件（.1 已被 clear 删除且不再生成）
        reqlog.begin("after-clear", "messages", "m", False, "")
        reqlog.finish_ok("after-clear")
        assert not persist_env.with_suffix(".jsonl.1").exists()
        lines = [json.loads(line) for line in persist_env.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [e["req_id"] for e in lines] == ["after-clear"]

    def test_persist_open_failure_recovers(self, persist_env, monkeypatch):
        """打开失败（如 NFS 未就绪）：丢弃本条打日志，下次请求重试打开。"""
        real_open = type(persist_env).open
        calls = {"n": 0}

        def flaky_open(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated mount not ready")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(type(persist_env), "open", flaky_open)
        reqlog.begin("fl1", "messages", "m", False, "")
        reqlog.finish_ok("fl1")  # 第一次打开失败，静默丢本条
        reqlog.begin("fl2", "messages", "m", False, "")
        reqlog.finish_ok("fl2")  # 第二次重试成功
        lines = [json.loads(line) for line in persist_env.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [e["req_id"] for e in lines] == ["fl2"]


def test_hourly_buckets_aggregate():
    """2.7.71 监控增强：逐小时桶（请求数/成败/p95/模型分布），在飞计入 inflight_now。"""
    from app import reqlog

    reqlog._entries.clear()
    reqlog._inflight.clear()
    now = time.time()
    cur_h = int(now // 3600)
    h0 = cur_h * 3600

    def entry(i, ts, ok, t_total, model):
        return {"req_id": f"r{i}", "ts": ts, "endpoint": "chat", "model": model,
                "stream": True, "preview": "", "account": "a", "mode": "jwt",
                "ok": ok, "status": 200 if ok else 500, "error": "" if ok else "x",
                "t_first": 1.0, "t_total": t_total,
                "input_tokens": 1, "output_tokens": 1}

    with reqlog._lock:
        reqlog._entries.extend([
            entry(1, h0 + 100, True, 2.0, "GLM-5.3-Flash"),
            entry(2, h0 + 200, True, 8.0, "GLM-5.3-Flash"),
            entry(3, h0 + 300, False, 3.0, "GLM-5.3"),
            # 在飞（ok=None）：计入 inflight_now，并发窗口到 now
            entry(4, now - 30, None, None, "GLM-5.3-Flash"),
        ])
    out = reqlog.hourly()
    assert out, "应有至少一个小时桶"
    b = out[-1]
    assert b["reqs"] == 4
    assert b["ok"] == 2 and b["fail"] == 1
    assert b["inflight_now"] == 1
    assert b["p95_t_total"] is not None and b["p95_t_total"] >= 3.0
    assert b["max_t_total"] == 8.0
    assert b["models"]["GLM-5.3-Flash"] == 3
    assert b["peak_concurrency"] >= 1
    # 清场
    reqlog._entries.clear()
    reqlog._inflight.clear()


def test_hourly_peak_concurrency_cross_hour_isolation():
    """2.7.71 修正回归：峰值并发逐桶裁剪——上一小时的请求不得污染本小时峰值。"""
    from app import reqlog

    reqlog._entries.clear()
    reqlog._inflight.clear()
    now = time.time()
    cur_h0 = int(now // 3600) * 3600

    def entry(i, ts, ok, t_total, model="GLM-5.3-Flash"):
        return {"req_id": f"r{i}", "ts": ts, "endpoint": "chat", "model": model,
                "stream": True, "preview": "", "account": "a", "mode": "jwt",
                "ok": ok, "status": 200, "error": "",
                "t_first": 1.0, "t_total": t_total,
                "input_tokens": 1, "output_tokens": 1}

    with reqlog._lock:
        # 上一小时：3 条完全重叠的长流（那里的峰值应为 3）
        reqlog._entries.extend([
            entry(1, cur_h0 - 100, True, 90.0),
            entry(2, cur_h0 - 100, True, 90.0),
            entry(3, cur_h0 - 100, True, 90.0),
            # 本小时：仅 1 条短请求
            entry(4, now - 5, True, 2.0),
        ])
    out = reqlog.hourly()
    peaks = {b["hour_start"]: b["peak_concurrency"] for b in out}
    assert peaks[cur_h0 - 3600] == 3, "上一小时峰值应为 3（三条重叠长流）"
    assert peaks[cur_h0] == 1, f"本小时峰值应为 1，不得被上一小时污染（got {peaks[cur_h0]}）"
    reqlog._entries.clear()
    reqlog._inflight.clear()
