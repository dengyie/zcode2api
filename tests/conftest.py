"""pytest 共享夹具。

设计要点（docs/testing/01 §可测性约束）：
- Mock 上游以真实 uvicorn 端口拉起（session 级），网关内部自建的 httpx 客户端
  直接走真实 TCP —— 与生产行为同形，SSE 流式也真实经过网络栈。
- 底座的 store 是模块级单例，gateway/admin/quota/auth_admin 各自绑定了名字，
  fresh_app 在所有绑定点重绑到同一个新实例，并隔离 data 目录。
- captcha_manager 被替换为桩（避免打到真实 zcode.z.ai）。
"""

from __future__ import annotations

import importlib
import os
import threading
import time

import pytest
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient

from app import settings
from tests.mock_upstream import server as mock_server_module

# 开发机常驻系统代理（如 Clash 监听 127.0.0.1:7897）时，httpx 默认 trust_env=True
# 会把发往 127.0.0.1 的请求也交给代理（httpx 不读 macOS 例外列表），断连场景会被
# 代理转译成 502。测试进程内全局屏蔽代理，保证网关→Mock 走真实 TCP。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")

# 底座中绑定了 store 名字的全部模块 —— 新增导入点时必须同步加入
_STORE_BINDING_MODULES = (
    "app.store",
    "app.captcha",
    "app.routes.gateway",
    "app.routes.admin_api",
    "app.quota",
    "app.auth_admin",
)


@pytest.fixture(scope="session")
def mock_server():
    """真实端口上的 Mock 上游。返回 (app, port)。

    用模块级 `mock_server_module.app`（含 _wrap_asgi 断连包装）而非裸 build_app()：
    connect_fail_first 的真断连判定发生在 FastAPI 之外的包装层。
    """
    app = mock_server_module.app
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started, "mock upstream failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield app, port
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def fresh_app(tmp_path, monkeypatch):
    """独立 app 实例：隔离 data 目录、重绑所有 store 绑定点。"""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ZCODE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings, "DB_PATH", data_dir / "accounts.db")
    monkeypatch.setattr(settings, "COOLING_SECONDS", 300)

    from app import store as store_module
    from app.store import Store

    fresh = Store()
    for mod_name in _STORE_BINDING_MODULES:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "store"):
            monkeypatch.setattr(mod, "store", fresh)
    assert store_module.store is fresh
    return fresh


class _StubCaptcha:
    """验证码桩：永不打真网。"""

    def __init__(self) -> None:
        self.paused_seconds: list[int] = []

    async def get_verify_param(self, port: int | None = None) -> tuple[str, str | None]:
        return "mock-verify-param", None

    def invalidate(self) -> None:
        pass

    def pause_refill(self, seconds: int) -> None:
        self.paused_seconds.append(seconds)


@pytest.fixture
def stub_captcha() -> _StubCaptcha:
    """验证码桩实例。gateway_client 依赖注入同一实例，测试可直接断言其调用。"""
    return _StubCaptcha()


@pytest_asyncio.fixture
async def gateway_client(fresh_app, mock_server, monkeypatch, stub_captcha):
    """挂好 Mock 上游的网关 ASGI 客户端。返回 (client, mock_app)。"""
    mock_app, port = mock_server
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(settings, "UPSTREAM", {
        "zai": f"{base}/api/v1/zcode-plan/anthropic/v1/messages",
        "zai_fallback": f"{base}/api/anthropic/v1/messages",
        "bigmodel": f"{base}/api/anthropic/v1/messages",
    })
    # 后台额度刷新也指向 Mock（billing/current|balance|usage）
    monkeypatch.setattr(settings, "ZCODE_BILLING_BASE", f"{base}/api/v1/zcode-plan")
    # OAuth（cli init/poll + api-key 兑换链）全部收敛到 Mock —— 测试永不打真网
    monkeypatch.setattr(settings, "OAUTH_API_BASE", f"{base}/api/v1")
    monkeypatch.setattr(settings, "ZAI_EXCHANGE_ORIGIN", base)

    from app.routes import gateway as gateway_module
    monkeypatch.setattr(gateway_module, "captcha_manager", stub_captcha)

    from app.main import create_app
    gateway = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=gateway), base_url="http://gateway.test"
    ) as client:
        yield client, mock_app


def seed_account(store, secret: str, name: str = "t"):
    return store.add_account("zai", name, secret)
