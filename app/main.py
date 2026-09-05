"""FastAPI 应用工厂 + 生命周期。"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import logs, settings
from .captcha import captcha_manager
from .quota import monitor
from .routes import admin_api, gateway, pages

# 修正 Windows 中文控制台可能出现的乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _display_host() -> str:
    # 0.0.0.0 / 空地址在浏览器中不可直接访问，展示为 127.0.0.1
    host = (settings.HOST or "").strip()
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor.start()
    captcha_manager.start()   # 验证码预解池后台补充
    base = f"http://{_display_host()}:{settings.PORT}"
    logs.banner([
        f"{logs._B}{logs._MAG}zcode-hub{logs._R} {logs._DIM}v{settings.APP_VERSION} · Python{logs._R}",
        f"{logs._DIM}后台管理{logs._R}  {logs._C}{base}/admin/login{logs._R}",
        f"{logs._DIM}对话端点{logs._R}  {logs._C}{base}/v1/messages{logs._R}",
    ])
    try:
        yield
    finally:
        await monitor.stop()
        await captcha_manager.close()


def create_app() -> FastAPI:
    app = FastAPI(title="zcode-hub", version=settings.APP_VERSION, lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=str(settings.FRONTEND_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(admin_api.router)
    app.include_router(gateway.router)
    return app


app = create_app()
