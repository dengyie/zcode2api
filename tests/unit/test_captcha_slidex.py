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
    param, _fail = await CaptchaManager()._run_solver("s", "cn", "p", egress.DIRECT)

    assert param == "slidex-param" and _fail is None
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


def test_slidex_fail_line_feeds_attribution(monkeypatch):
    """SLIDEX_FAIL=<code> 归因（2.7.50 review P3 → 2.7.63 局部化）：正常退出
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
        param, fail = await mgr._run_solver_slidex("scene", "cn", "pfx", "direct", None)
        assert param is None
        assert fail == "slidex 求解未果（F001）"

    _aio.run(main())


def test_degraded_stops_retry_loop(monkeypatch):
    """2.7.60 环境级失败快速止损：SLIDEX_FAIL=TRACELESS:DEGRADED（阿里云对
    headless 判定收紧，2026-11-18 深夜实弹：TP 出口与直连 CN 同窗全灭）时
    重试无意义——必须只跑 1 次求解进程即止损，不再 4 连重试白烧 16 分钟。"""
    import asyncio as _aio

    mgr = CaptchaManager()
    exec_calls: list = []

    class _Proc:
        pid = 12348

        async def communicate(self):
            return b"SLIDEX_FAIL=TRACELESS:DEGRADED", b""

        def kill(self):
            pass

    async def fake_comm(self, proc):
        return await proc.communicate()

    async def fake_cleanup(self, proc):
        pass

    async def fake_exec(*a, **kw):
        exec_calls.append(1)
        return _Proc()

    monkeypatch.setattr(CaptchaManager, "_communicate_solver", fake_comm)
    monkeypatch.setattr(CaptchaManager, "_cleanup_solver", fake_cleanup)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def main():
        token, msg = await mgr._solve_one(
            {"sceneId": "s", "region": "cn", "prefix": "p"}, "direct", None
        )
        assert token is None
        assert len(exec_calls) == 1  # 止损：1 次即停（非 CAPTCHA_SOLVE_RETRIES 次）
        assert "DEGRADED" in (msg or "")

    _aio.run(main())


def test_slidex_stderr_surfaces_on_failure(monkeypatch):
    """2.7.58 诊断透传：失败时 runner stderr 尾部（fatal/归因）必须进日志。

    2026-11-17 领取连败排查实证：stderr=DEVNULL 吞掉 slidex 的
    `fatal: TimeoutError: Page.goto ...`，故障窗口只剩笼统「多次重试无结果」，
    只能盲猜。成功路径不产生日志（防刷屏）。"""
    import asyncio as _aio

    mgr = CaptchaManager()
    warns: list[str] = []

    class _Proc:
        pid = 12346

        async def communicate(self):
            return (
                b"SLIDEX_FAIL=EXC",
                b"INFO log line\n[slidex] attempt 1: traceless=None\n"
                b"[slidex] fatal: TimeoutError: Page.goto: Timeout 30000ms exceeded.\n",
            )

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
    monkeypatch.setattr(
        "app.captcha.logs.warn",
        lambda tag, msg: warns.append(msg),
    )

    async def main():
        param, _fail = await mgr._run_solver_slidex("scene", "cn", "pfx", "direct", None)
        assert param is None
        assert len(warns) == 1
        # 只取尾部 3 行且过滤空行：fatal 行必须在，INFO 行保留在尾部窗口内
        assert "Page.goto" in warns[0]

    _aio.run(main())


def test_slidex_stderr_silent_on_success(monkeypatch):
    """求解成功（含重试后成功）：stderr 诊断不得产生日志（防刷屏）。"""
    import asyncio as _aio

    mgr = CaptchaManager()
    warns: list[str] = []

    class _Proc:
        pid = 12347

        async def communicate(self):
            return (b"VERIFY_PARAM=ok\n", b"[slidex] attempt 1 failed; retrying\n")

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
    monkeypatch.setattr(
        "app.captcha.logs.warn",
        lambda tag, msg: warns.append(msg),
    )

    async def main():
        param, _fail = await mgr._run_solver_slidex("scene", "cn", "pfx", "direct", None)
        assert param == "ok" and _fail is None
        assert warns == []

    _aio.run(main())


def test_solve_timeout_yields_attribution(monkeypatch):
    """2.7.63 review P2#5：240s SIGKILL 超时路径必须归因（此前 return None
    不写任何归因 → 首拍超时即全程「多次重试无结果」盲区，1117 实弹源头）。"""
    import asyncio as _aio

    mgr = CaptchaManager()

    class _Proc:
        pid = 12349

        async def communicate(self):
            raise TimeoutError()

        def kill(self):
            pass

    async def fake_cleanup(self, proc):
        pass

    async def fake_exec(*a, **kw):
        return _Proc()

    monkeypatch.setattr(CaptchaManager, "_communicate_solver",
                        lambda self, proc: proc.communicate())
    monkeypatch.setattr(CaptchaManager, "_cleanup_solver", fake_cleanup)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def main():
        param, fail = await mgr._run_solver_slidex("scene", "cn", "pfx", "direct", None)
        assert param is None
        assert "超时" in (fail or "") and "SIGKILL" in (fail or "")

        token, msg = await mgr._solve_one(
            {"sceneId": "s", "region": "cn", "prefix": "p"}, "direct", None
        )
        assert token is None and "超时" in (msg or "")

    _aio.run(main())


def test_timeout_streak_stops_retry_loop(monkeypatch):
    """2.7.68 链式超时止损：DEADLINE / TRACELESS:TIMEOUT 等超时型归因连续
    CAPTCHA_TIMEOUT_GIVEUP_AFTER 次（换过 context/进程仍超时）= 环境级失败，
    第 N 次起不再白烧（11-18 晚 159 次求解 0 成功、每号白烧 16 分钟的主形态）。"""
    import asyncio as _aio

    mgr = CaptchaManager()
    exec_calls: list = []

    class _Proc:
        pid = 12349

        async def communicate(self):
            return b"SLIDEX_FAIL=DEADLINE", b""

        def kill(self):
            pass

    async def fake_comm(self, proc):
        return await proc.communicate()

    async def fake_cleanup(self, proc):
        pass

    async def fake_exec(*a, **kw):
        exec_calls.append(1)
        return _Proc()

    monkeypatch.setattr(CaptchaManager, "_communicate_solver", fake_comm)
    monkeypatch.setattr(CaptchaManager, "_cleanup_solver", fake_cleanup)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(settings, "CAPTCHA_SOLVE_RETRIES", 4)
    monkeypatch.setattr(settings, "CAPTCHA_TIMEOUT_GIVEUP_AFTER", 2)

    async def main():
        token, msg = await mgr._solve_one(
            {"sceneId": "s", "region": "cn", "prefix": "p"}, "direct", None
        )
        assert token is None
        # 止损：连续 2 次超时即停（非 4 次跑满）
        assert len(exec_calls) == 2
        assert "DEADLINE" in (msg or "")

    _aio.run(main())


def test_timeout_streak_resets_on_non_timeout(monkeypatch):
    """非超时归因（F 码行为风控）打断超时连击计数：F001 后换 context 重试
    仍有价值（2.7.66/67 手势/指纹修复正打这类形态），不得被超时止损误伤。"""
    import asyncio as _aio

    mgr = CaptchaManager()
    exec_calls: list = []

    class _Proc:
        pid = 12350

        def __init__(self, fail: bytes):
            self._fail = fail

        async def communicate(self):
            return self._fail, b""

        def kill(self):
            pass

    procs: list[bytes] = [b"SLIDEX_FAIL=DEADLINE", b"SLIDEX_FAIL=DRAG:F001",
                          b"SLIDEX_FAIL=DEADLINE", b"SLIDEX_FAIL=DEADLINE"]

    async def fake_comm(self, proc):
        return await proc.communicate()

    async def fake_cleanup(self, proc):
        pass

    async def fake_exec(*a, **kw):
        exec_calls.append(1)
        return _Proc(procs[len(exec_calls) - 1])

    monkeypatch.setattr(CaptchaManager, "_communicate_solver", fake_comm)
    monkeypatch.setattr(CaptchaManager, "_cleanup_solver", fake_cleanup)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(settings, "CAPTCHA_SOLVE_RETRIES", 4)
    monkeypatch.setattr(settings, "CAPTCHA_TIMEOUT_GIVEUP_AFTER", 2)

    async def main():
        token, msg = await mgr._solve_one(
            {"sceneId": "s", "region": "cn", "prefix": "p"}, "direct", None
        )
        assert token is None
        # 超时(F1) → F001 清零 → 超时(F3) → 超时(F4) 达阈值止损 = 4 次跑满
        assert len(exec_calls) == 4
        assert "DEADLINE" in (msg or "")

    _aio.run(main())


def test_get_verify_param_retries_override_reaches_solve(monkeypatch):
    """claim 链求解预算解耦（2.7.68）：get_verify_param(retries=2) 必须传到
    _solve_one 的 max_retries——预算不生效等于没解耦。"""
    import asyncio as _aio

    mgr = CaptchaManager()
    seen: dict = {}

    async def _config(tag=None, proxy=None):
        return {"sceneId": "s", "region": "cn", "prefix": "p"}

    async def _solve(config, tag=egress.DIRECT, proxy=None, max_retries=None):
        seen["max_retries"] = max_retries
        return None, "SLIDEX_FAIL=DEADLINE"

    monkeypatch.setattr(mgr, "fetch_config", _config)
    monkeypatch.setattr(mgr, "_solve_one", _solve)

    async def main():
        try:
            await mgr.get_verify_param(egress_tag=egress.DIRECT, retries=2)
        except Exception:  # noqa: BLE001 - 求解失败正常上抛，断言只看透传
            pass
        assert seen["max_retries"] == 2

    _aio.run(main())
