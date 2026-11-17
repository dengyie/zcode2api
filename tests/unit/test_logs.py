"""logs.py 时间戳前缀——排障时间线必须一眼可读（2026-11-14 边缘 405 复盘）。"""

import re
import time

import pytest

from app import logs

TS_RE = re.compile(r"\[\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")


def test_warn_line_has_timestamp(capsys):
    logs.warn("16fb8976e9e4b2ca", "账号 x 模型并发已满（3008/3009/3010），4s 后重试（1/5）")
    out = capsys.readouterr().out
    assert TS_RE.search(out)
    assert "[~]" in out
    assert "16fb8976e9e4b2ca" in out


def test_req_lifecycle_lines_have_timestamp(capsys):
    logs.req("16fb8976", "GLM-5.3-Flash", True, "<system-reminder>")
    logs.req_ok("16fb8976")
    logs.req_err("16fb8976", "闸门 edge_hold，拒绝本请求（Retry-After 168s）")
    out = capsys.readouterr().out
    assert len(TS_RE.findall(out)) == 3


def test_ok_info_err_have_timestamp(capsys):
    logs.ok("store", "loaded 7 accounts")
    logs.info("16fb8976", "client=local ua=Python-urllib/3.11")
    logs.err("16fb8976", "上游边缘封禁（IP 级），全池暂停 plan 渠道 180s")
    out = capsys.readouterr().out
    assert len(TS_RE.findall(out)) == 3


def test_banner_has_no_timestamp(capsys):
    logs.banner(["zcode-hub 2.7.34"])
    out = capsys.readouterr().out
    assert not TS_RE.search(out)
    assert "zcode-hub 2.7.34" in out


# ── opslog 结构化落盘（2.7.64）────────────────────────────────────────────

def _reset_logs_state(monkeypatch, target):
    """重置 logs.py opslog 模块态并指向 target。

    ⚠️ 重置 _fh 前必须先 close 真实句柄：monkeypatch 会把旧句柄存进 undo
    列表持有引用（不触发 GC 关闭），Windows 上未关闭的句柄会让后续日切
    rename 报 PermissionError（独立脚本无此问题——置 None 即引用归零）。
    """
    old = logs._fh
    if old is not None:
        try:
            old.close()
        except OSError:
            pass
    monkeypatch.setattr(logs, "_fh", None)
    monkeypatch.setattr(logs, "_current_day", "")
    monkeypatch.setattr(logs, "_enabled", None)
    monkeypatch.setattr(logs, "_path", None)


@pytest.fixture()
def _opslog_env(tmp_path, monkeypatch):
    """opslog 落盘到临时文件并重置 logs.py 模块态（句柄/日切判据）。"""
    from app import settings

    target = tmp_path / "opslog.jsonl"
    _reset_logs_state(monkeypatch, target)
    # 直接注入 settings 侧开关与路径（logs._opslog 惰性读取）
    monkeypatch.setattr(settings, "OPSLOG_PERSIST", True)
    monkeypatch.setattr(settings, "OPSLOG_PATH", target)
    yield target


def test_opslog_persists_structured_entry(_opslog_env):
    import json as _json

    logs.warn("captcha", "求解失败: 多次重试无结果")
    lines = _opslog_env.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    e = _json.loads(lines[0])
    assert e["level"] == "warn"
    assert e["tag"] == "captcha"
    assert "求解失败" in e["msg"]
    # 时间戳含年份（跨日排查切片口径，2.7.58-61 实战提炼）
    assert len(e["_ts"]) == 19 and e["_ts"][4] == "-"


def test_opslog_all_levels_flow(_opslog_env):
    import json as _json

    logs.ok("store", "loaded")
    logs.info("claim", "轮次开始")
    logs.warn("egress_pool", "rebalance 去抖")
    logs.err("claim", "领取失败")
    levels = [
        _json.loads(line)["level"]
        for line in _opslog_env.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert levels == ["ok", "info", "warn", "err"]


def test_opslog_disabled_when_persist_off(tmp_path, monkeypatch):
    from app import settings

    target = tmp_path / "opslog.jsonl"
    _reset_logs_state(monkeypatch, target)
    monkeypatch.setattr(settings, "OPSLOG_PERSIST", False)
    monkeypatch.setattr(settings, "OPSLOG_PATH", target)

    logs.warn("captcha", "不应落盘")
    assert not target.exists()


def test_opslog_day_rotation_keeps_one_generation(_opslog_env, monkeypatch):
    """日切：昨日文件轮转 .1（保留一代），新条目进当日文件。"""
    import json as _json

    logs.warn("claim", "昨日事件")
    # 伪造文件首行时间戳为昨日 + 重置句柄（模拟跨日；先 close 防 Windows 句柄占用）
    first = _json.loads(_opslog_env.read_text(encoding="utf-8").strip())
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    first["_ts"] = f"{yesterday} 23:59:59"
    _opslog_env.write_text(
        _json.dumps(first, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _reset_logs_state(monkeypatch, _opslog_env)

    logs.warn("claim", "今日事件")
    lines = _opslog_env.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert "今日事件" in lines[0]
    rot = _opslog_env.with_suffix(_opslog_env.suffix + ".1")
    assert rot.exists()
    assert "昨日事件" in rot.read_text(encoding="utf-8")


def test_opslog_write_failure_degrades_silently(_opslog_env, monkeypatch):
    """落盘失败（磁盘满等）：stdout 照常、不抛异常、句柄作废待重试。"""
    from app import logs as logs_mod

    # 先正常开一次句柄，再把 write 换成炸点
    logs.warn("captcha", "先成功一条")
    assert _opslog_env.exists()
    assert logs_mod._fh is not None

    class _BrokenFh:
        def write(self, *_a):
            raise OSError("disk full")

        def flush(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(logs_mod, "_fh", _BrokenFh())
    logs.err("captcha", "写失败也不崩")
    # 句柄已被重置为 None（下次重试打开）
    assert logs_mod._fh is None
