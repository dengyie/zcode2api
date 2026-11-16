"""slidex solver 分流（2026-11-16 活动套餐领券事故根修）。

事故链：Chrome `--proxy-server` 不支持带凭证代理 URL
（http://user:pass@host:port，TP per-account 供给即此形态）→
ERR_NO_SUPPORTED_PROXIES → AliyunCaptcha.js SDK 加载失败 → waitForFunction
20s 超时 → 求解永远失败 → 9/11 号 3 亿 token 活动套餐领不到。

slidex 路线（缺省）：python runner + playwright context 级 proxy（原生支持
username/password），复用 slidex `aliyun-captcha-js` provider（z.ai 实测内化
内核）。出口对齐口径必须与 pw 路线逐字一致：proxy 参数优先 → tag=proxy 取
env → 都没有则响亮失败（静默求解 = token 出口错配 = 3012）。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app import egress, settings
from app.captcha import CaptchaManager

_TP_PROXY = "http://zh-acc-abc__type_clash_proxy__region_us-west:tp-secret@127.0.0.1:9101"


class _FakeProc:
    pid = 4321

    def kill(self) -> None:
        pass

    async def communicate(self):
        return b"VERIFY_PARAM=slidex-param\n", b""

    async def wait(self):
        return 0


@pytest.fixture(autouse=True)
def _slidex_mode(monkeypatch):
    monkeypatch.setattr(settings, "CAPTCHA_SOLVER_MODE", "slidex")
    monkeypatch.setattr(settings, "CAPTCHA_SOLVER_PY", settings.CAPTCHA_SOLVER_DIR / "slidex_runner.py")


async def test_slidex_is_default_mode(monkeypatch):
    """缺省走 slidex：create_subprocess_exec 第一个参数必须是 python 本体。"""
    seen: dict = {}

    async def _fake_exec(*args, **kwargs):
        seen["args"] = args
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    param = await CaptchaManager()._run_solver("s", "cn", "p", egress.DIRECT)

    assert param == "slidex-param"
    assert seen["args"][0] == sys.executable
    assert str(settings.CAPTCHA_SOLVER_PY) in str(seen["args"][2])


async def test_slidex_credentialed_proxy_flows_to_env(monkeypatch):
    """TP 带凭证代理必须整条传给 runner（runner 侧再拆成 playwright proxy）。"""
    seen: dict = {}

    async def _fake_exec(*args, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    await CaptchaManager()._run_solver("s", "cn", "p", egress.PROXY, proxy=_TP_PROXY)

    assert seen["env"]["HTTPS_PROXY"] == _TP_PROXY
    assert seen["env"]["HTTP_PROXY"] == _TP_PROXY


async def test_slidex_proxy_tag_without_env_fails_loud(monkeypatch):
    """tag=proxy 但无代理可用：响亮失败，绝不静默直连求解（出口错配=3012）。"""

    async def _fake_exec(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("响亮失败路径不得启动求解进程")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr("app.egress.proxy_url", lambda url=None: None)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    with pytest.raises(RuntimeError, match="无法对齐验证码出口"):
        await CaptchaManager()._run_solver("s", "cn", "p", egress.PROXY, proxy=None)


async def test_slidex_missing_runner_raises_runtime_error(monkeypatch):
    """runner 文件缺失：启动即失败并指名路径，不留静默 4 连重试。"""
    monkeypatch.setattr(
        settings, "CAPTCHA_SOLVER_PY", settings.CAPTCHA_SOLVER_DIR / "not-exists.py"
    )
    with pytest.raises(RuntimeError, match="slidex_runner|not-exists"):
        await CaptchaManager()._run_solver("s", "cn", "p", egress.DIRECT)


async def test_pw_mode_still_routes_to_node(monkeypatch):
    """ZCODE_CAPTCHA_SOLVER=pw 回滚位：仍走 node + solver_pw.js。"""
    seen: dict = {}

    async def _fake_exec(*args, **kwargs):
        seen["args"] = args
        return _FakeProc()

    monkeypatch.setattr(settings, "CAPTCHA_SOLVER_MODE", "pw")
    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    await CaptchaManager()._run_solver("s", "cn", "p", egress.DIRECT)

    assert seen["args"][0] == settings.NODE_PATH
    assert str(settings.CAPTCHA_SOLVER_JS) in str(seen["args"][1])


def test_runner_parses_credentialed_proxy(monkeypatch):
    """runner 侧 URL 拆解：带凭证 TP URL → playwright proxy 三元组。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "slidex_runner", settings.CAPTCHA_SOLVER_PY
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setenv("HTTPS_PROXY", _TP_PROXY)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    p = mod._playwright_proxy()
    assert p["server"] == "http://127.0.0.1:9101"
    assert p["username"].startswith("zh-acc-abc")
    assert p["password"] == "tp-secret"

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    assert mod._playwright_proxy() is None


def test_slidex_fail_line_feeds_last_error(monkeypatch):
    """SLIDEX_FAIL=<code> 解析入 _last_error（2.7.50 review P3）：正常退出
    无 param 的路径失败文案不再是笼统「多次重试无结果」，可归因 F001 等。"""
    import asyncio as _aio

    mgr = CaptchaManager()

    class _Proc:
        pid = 12345

        async def communicate(self):
            return b"SLIDEX_FAIL=F001", b""

        def kill(self):
            pass

    async def fake_comm(self, proc):
        return await proc.communicate()

    async def fake_cleanup(self, proc):
        pass

    async def fake_exec(*a, **kw):
        return _Proc()

    monkeypatch.setattr(CaptchaManager, "_communicate_solver", fake_comm)
    monkeypatch.setattr(CaptchaManager, "_cleanup_solver", fake_cleanup)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def main():
        param = await mgr._run_solver_slidex("scene", "cn", "pfx", "direct", None)
        assert param is None
        assert mgr._last_error == "slidex 求解未果（F001）"

    _aio.run(main())
