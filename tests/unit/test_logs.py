"""logs.py 时间戳前缀——排障时间线必须一眼可读（2026-11-14 边缘 405 复盘）。"""

import re

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
