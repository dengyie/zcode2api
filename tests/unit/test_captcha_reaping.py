"""求解器子进程收尾：无论成败都不得留下 node / Chromium。

背景（PR #12 实测）：Chromium 启动后会 setsid 脱离 node 的进程组，
node 被 SIGKILL（求解超时）或同步挂死时，killpg(node 组) 打不到逃逸的
Chromium——单实例 ~360MB，小内存机器上十分钟可泄漏数 GB。调用侧必须
无条件整组清场，并按孤儿特征兜底清扫。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app import egress, settings
from app.captcha import CaptchaManager


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    """绝不真杀进程：killpg 记录调用，孤儿清扫返回 0。

    Windows 无 os.killpg（POSIX 专属）：stub 不存在，运行时走 app 内
    except AttributeError → proc.kill() 退化路径（fake 补 kill() 即可覆写）。
    """
    pgids: list[int] = []
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: pgids.append(pgid))
    monkeypatch.setattr("app.captcha._kill_orphan_chromium", lambda: 0)
    return pgids


class _FakeProc:
    pid = 4321

    def kill(self) -> None:
        pass  # Windows 退化路径（无 killpg）：杀 node 本体

    async def communicate(self):
        return b"VERIFY_PARAM=mock\n", b""

    async def wait(self):
        return 0


class _HangingProc:
    pid = 4321

    def kill(self) -> None:
        pass

    async def communicate(self):
        await asyncio.sleep(3600)
        return b"", b""

    async def wait(self):
        return -9


async def test_solver_starts_in_own_session(monkeypatch):
    """独立进程组：超时后才能 killpg 整组（连 Chromium 一起）清场。"""
    seen: dict = {}

    async def _fake_exec(*args, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    await CaptchaManager()._run_solver("s", "cn", "p", egress.DIRECT)

    assert seen.get("start_new_session") is True


@pytest.mark.skipif(os.name == "nt",
                    reason="killpg 整组语义 POSIX 专属；Windows 退化 proc.kill，无进程组可断言")
async def test_timeout_kills_whole_process_group(monkeypatch, _no_real_processes):
    """超时：killpg 整组，而不是只杀 node 本体把自己留下。"""

    async def _fake_exec(*args, **kwargs):
        return _HangingProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(settings, "CAPTCHA_SOLVE_TIMEOUT", 0.01)

    param = await CaptchaManager()._run_solver("s", "cn", "p", egress.DIRECT)

    assert param is None
    assert _no_real_processes == [4321]


async def test_solver_deadline_hint_is_earlier_than_caller_timeout(monkeypatch):
    """solver 侧硬超时必须早于调用方 SIGKILL，否则逃逸 Chromium 无人清场。"""
    seen: dict = {}

    async def _fake_exec(*args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(settings, "CAPTCHA_SOLVE_TIMEOUT", 240)

    await CaptchaManager()._run_solver("s", "cn", "p", egress.DIRECT)

    assert seen["ZCODE_SOLVE_DEADLINE_MS"] == str((240 - 20) * 1000)
