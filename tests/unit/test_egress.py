"""出口一致性：验证码 token 的签发出口必须等于请求出口。

背景（2026-10-30 实测）：token 直连解、messages 走 10822 代理 → 上游判
`unusual activity`（3012）。官方客户端天然同出口，本服务必须显式对齐。
见 app/egress.py 与 app/captcha.py 的按出口分池。
"""

from __future__ import annotations

import httpx
import pytest

from app import constants, egress
from app.captcha import CaptchaManager, _Token

_ZAI = "https://zcode.z.ai/api/v1/x"


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    """屏蔽真实 getproxies（macOS 会读系统代理），由各用例显式给定。"""
    monkeypatch.setattr(egress, "getproxies", lambda: {})


def _env(monkeypatch, *, no: str = "", **proxies: str) -> None:
    info = dict(proxies)
    if no:
        info["no"] = no
    monkeypatch.setattr(egress, "getproxies", lambda: dict(info))


class TestEgressResolution:
    def test_no_proxy_configured_is_direct(self):
        assert egress.egress_for(_ZAI) == egress.DIRECT

    def test_proxy_configured_routes_through_proxy(self, monkeypatch):
        _env(monkeypatch, https="http://127.0.0.1:10822")
        assert egress.egress_for(_ZAI) == egress.PROXY

    def test_all_proxy_also_counts(self, monkeypatch):
        _env(monkeypatch, all="socks5://127.0.0.1:1080")
        assert egress.egress_for(_ZAI) == egress.PROXY
        assert egress.proxy_url(_ZAI) == "socks5://127.0.0.1:1080"

    def test_http_only_proxy_does_not_cover_https(self, monkeypatch):
        """httpx 的 http:// mount 只匹配 http 目标，https 目标仍直连。"""
        _env(monkeypatch, http="http://a:1")
        assert egress.egress_for(_ZAI) == egress.DIRECT
        assert egress.egress_for("http://zcode.z.ai/api/v1/x") == egress.PROXY

    def test_https_proxy_preferred_over_all(self, monkeypatch):
        _env(monkeypatch, https="http://b:2", all="socks5://c:3")
        assert egress.proxy_url(_ZAI) == "http://b:2"

    def test_no_proxy_host_bypasses_proxy(self, monkeypatch):
        _env(monkeypatch, https="http://127.0.0.1:10822", no="127.0.0.1,localhost,zcode.z.ai")
        assert egress.egress_for(_ZAI) == egress.DIRECT

    def test_bare_domain_covers_apex_and_subdomain(self, monkeypatch):
        _env(monkeypatch, no="zcode.z.ai")
        assert egress.no_proxy_covers(_ZAI) is True
        assert egress.no_proxy_covers("https://sub.zcode.z.ai/x") is True

    def test_leading_dot_covers_subdomain_only(self, monkeypatch):
        """与 curl 不同：httpx 下 `.zcode.z.ai` 不覆盖 apex。"""
        _env(monkeypatch, no=".zcode.z.ai")
        assert egress.no_proxy_covers(_ZAI) is False
        assert egress.no_proxy_covers("https://sub.zcode.z.ai/x") is True

    def test_glob_form_matches_nothing(self, monkeypatch):
        """httpx 把 `*.zcode.z.ai` 的 `*` re.escape 成字面量，故不命中。"""
        _env(monkeypatch, no="*.zcode.z.ai")
        assert egress.no_proxy_covers(_ZAI) is False
        assert egress.no_proxy_covers("https://sub.zcode.z.ai/x") is False

    def test_star_bypasses_everything(self, monkeypatch):
        _env(monkeypatch, https="http://127.0.0.1:10822", no="*")
        assert egress.egress_for(_ZAI) == egress.DIRECT
        assert egress.egress_for("https://anything.example/x") == egress.DIRECT

    def test_no_proxy_does_not_overmatch(self, monkeypatch):
        _env(monkeypatch, no="notzcode.z.ai")
        assert egress.no_proxy_covers(_ZAI) is False

    def test_entry_port_must_match(self, monkeypatch):
        _env(monkeypatch, no="zcode.z.ai:8443")
        assert egress.no_proxy_covers(_ZAI) is False  # 默认 443
        assert egress.no_proxy_covers("https://zcode.z.ai:8443/x") is True

    def test_localhost_entry(self, monkeypatch):
        _env(monkeypatch, https="http://127.0.0.1:10822", no="127.0.0.1,localhost")
        assert egress.egress_for("http://127.0.0.1:8790/admin") == egress.DIRECT
        assert egress.egress_for("http://localhost:8790/admin") == egress.DIRECT

    def test_case_insensitive_host(self, monkeypatch):
        _env(monkeypatch, https="http://p:1", no="ZCODE.Z.AI")
        assert egress.egress_for("https://ZCODE.z.ai/x") == egress.DIRECT

    @pytest.mark.parametrize("no", [
        "", "zcode.z.ai", ".zcode.z.ai", "*.zcode.z.ai", "*", "sub.zcode.z.ai",
        "127.0.0.1,localhost", "zcode.z.ai:443", "zcode.z.ai:8443",
        "192.168.0.0/16", "notzcode.z.ai", "ZCODE.Z.AI",
    ])
    @pytest.mark.parametrize("proxies", [
        {"https": "http://127.0.0.1:10822"},
        {"http": "http://a:1"},
        {"all": "socks5://c:3"},
        {"http": "http://a:1", "https": "http://b:2"},
    ])
    @pytest.mark.parametrize("url", [
        "https://zcode.z.ai/api/v1/x",
        "https://sub.zcode.z.ai/x",
        "http://zcode.z.ai/api/v1/x",
        "https://zcode.z.ai:8443/x",
        "https://open.bigmodel.cn/api/anthropic/v1/messages",
    ])
    def test_matches_httpx_resolution(self, monkeypatch, no, proxies, url):
        """与 httpx 真实的 mount 选择逐条对表（no_proxy 语义极易写错）。"""
        info = dict(proxies)
        if no:
            info["no"] = no
        monkeypatch.setattr(egress, "getproxies", lambda: dict(info))
        monkeypatch.setattr(httpx._utils, "getproxies", lambda: dict(info))
        assert egress.egress_for(url) == _httpx_egress(url)


def _httpx_egress(url: str) -> str:
    """httpx 视角下的出口：取排序后第一个命中的 mount，None 即直连。"""
    from httpx._utils import URLPattern, get_environment_proxies

    target = httpx.URL(url)
    mounts = sorted((URLPattern(key), proxy) for key, proxy in get_environment_proxies().items())
    for pattern, proxy in mounts:
        if pattern.matches(target):
            return egress.DIRECT if proxy is None else egress.PROXY
    return egress.DIRECT


class TestCaptchaEgressSeparation:
    """token 只能用在签发它的那个出口上。"""

    def _manager(self, monkeypatch, hot: str, solved: list[str]) -> CaptchaManager:
        mgr = CaptchaManager()

        async def _config(tag=None):
            return {"sceneId": "s", "region": "cn", "prefix": "p"}

        async def _solve(config, tag=egress.DIRECT):
            solved.append(tag)
            return _Token(f"tok-{tag}", "cn", tag)

        monkeypatch.setattr(mgr, "fetch_config", _config)
        monkeypatch.setattr(mgr, "_solve_one", _solve)
        monkeypatch.setattr(CaptchaManager, "hot_egress", lambda self: hot)

        async def _no_refill(need, tag=None):
            return None

        monkeypatch.setattr(mgr, "_refill_batch", _no_refill)
        return mgr

    async def test_non_hot_egress_solves_on_demand(self, monkeypatch):
        solved: list[str] = []
        mgr = self._manager(monkeypatch, hot=egress.PROXY, solved=solved)
        param, region = await mgr.get_verify_param(egress_tag=egress.DIRECT)
        assert param == "tok-direct"
        assert region == "cn"
        assert solved == [egress.DIRECT]

    async def test_hot_egress_uses_pool(self, monkeypatch):
        solved: list[str] = []
        mgr = self._manager(monkeypatch, hot=egress.PROXY, solved=solved)
        mgr._put(_Token("tok-proxy", "cn", egress.PROXY))
        param, _ = await mgr.get_verify_param(egress_tag=egress.PROXY)
        assert param == "tok-proxy"
        assert solved == []  # 未现解

    async def test_token_from_other_egress_is_not_served(self, monkeypatch):
        """池里混进别的出口的 token 时必须丢弃并现解，不能将就用。"""
        solved: list[str] = []
        mgr = self._manager(monkeypatch, hot=egress.PROXY, solved=solved)
        mgr._put(_Token("tok-direct-stale", "cn", egress.DIRECT))
        param, _ = await mgr.get_verify_param(egress_tag=egress.PROXY)
        assert param == "tok-proxy"
        assert solved == [egress.PROXY]

    async def test_invalidate_only_clears_target_egress(self, monkeypatch):
        solved: list[str] = []
        mgr = self._manager(monkeypatch, hot=egress.PROXY, solved=solved)
        mgr._put(_Token("tok-proxy", "cn", egress.PROXY))
        mgr._put(_Token("tok-direct", "cn", egress.DIRECT))
        mgr.invalidate(egress.PROXY)
        assert mgr._size(egress.PROXY) == 0
        assert mgr._size(egress.DIRECT) == 1

    async def test_solver_env_follows_egress(self, monkeypatch):
        """求解器子进程的代理变量：direct 剥掉、proxy 注入。"""
        _env(monkeypatch, https="http://127.0.0.1:10822")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10822")
        mgr = CaptchaManager()
        captured: dict[str, dict] = {}

        class _FakeProc:
            async def communicate(self):
                return b"VERIFY_PARAM=mock\n", b""

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env") or {}
            return _FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)

        await mgr._run_solver("s", "cn", "p", egress.DIRECT)
        assert "HTTP_PROXY" not in captured["env"]
        assert "HTTPS_PROXY" not in captured["env"]

        await mgr._run_solver("s", "cn", "p", egress.PROXY)
        assert captured["env"].get("HTTP_PROXY") == "http://127.0.0.1:10822"
        assert captured["env"].get("HTTPS_PROXY") == "http://127.0.0.1:10822"

    async def test_proxy_egress_without_proxy_env_raises(self, monkeypatch):
        mgr = CaptchaManager()
        with pytest.raises(RuntimeError):
            await mgr._run_solver("s", "cn", "p", egress.PROXY)


class TestModelRequestCaptchaGating:
    """`skip_model_request` 语义：官方在模型请求上带不带验证码的等价物。"""

    def _manager(self, monkeypatch, config: dict) -> CaptchaManager:
        mgr = CaptchaManager()

        async def _config(tag=None):
            return dict(config)

        monkeypatch.setattr(mgr, "fetch_config", _config)
        return mgr

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            # 线上实测：enabled + skip_model_request=true ⇒ 模型请求不带码
            ({"enabled": True, "skip_model_request": True}, False),
            ({"enabled": True, "skip_model_request": False}, True),
            # 字段缺失 = 官方「无配置即要码」语义
            ({"enabled": True}, True),
            ({"skip_model_request": True}, False),
            # enabled=false 时整站免码
            ({"enabled": False, "skip_model_request": False}, False),
        ],
    )
    async def test_config_matrix(self, monkeypatch, config, expected):
        mgr = self._manager(monkeypatch, config)
        assert await mgr.model_requests_need_captcha(egress.PROXY) is expected

    async def test_fetch_failure_falls_back_to_requiring_captcha(self, monkeypatch):
        """拉取失败兜底也必须「要码」：兜底字典里 skip_model_request 显式为 False。"""
        mgr = CaptchaManager()

        class _BoomClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr("app.captcha.httpx.AsyncClient", _BoomClient)
        config = await mgr.fetch_config(egress.PROXY)
        assert config == constants.CAPTCHA_DEFAULTS
        assert config["skip_model_request"] is False
        assert await mgr.model_requests_need_captcha(egress.PROXY) is True
