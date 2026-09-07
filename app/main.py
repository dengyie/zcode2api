"""FastAPI 应用工厂 + 生命周期。"""

from __future__ import annotations

import asyncio
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


def _backfill_fingerprints() -> int:
    """启动时给无指纹的存量账号补配（入池于旧版本）并落库。返回补配数。"""
    from .fingerprint import profile_for
    from .store import store

    backfilled = 0
    for account in store.list_accounts():
        if not isinstance(account.fingerprint, dict) or not account.fingerprint.get("device_mid"):
            profile_for(account)  # 懒分配（内存态为 DeviceProfile）
            store._assign_fingerprint(account)  # 固化为 dict 形态，与 add_account 一致
            store.update_account(account)
            backfilled += 1
    return backfilled


# 启动安装序的后台任务引用：事件循环对 task 只持弱引用（asyncio 官方文档），
# 不保存引用任务可能被 GC 中途丢弃且无日志 —— 与 captcha._refill_task 同一模式
_install_task: asyncio.Task | None = None


def _run_install_sequence_on_start() -> None:
    """启动后台执行一次安装序（官方客户端每次启动都拉 configs + 发 app_launch，
    日活去重在上游按 device_mid+日期）。失败只留痕，绝不影响启动。"""
    global _install_task

    from . import install

    async def _run() -> None:
        try:
            await install.run_install_sequence()
        except Exception as err:  # noqa: BLE001 —— 后台任务异常无人接收，必须自兜
            logs.err("install", f"安装序意外异常: {err}")

    _install_task = asyncio.create_task(_run())


@asynccontextmanager
async def lifespan(app: FastAPI):
    backfilled = _backfill_fingerprints()
    if backfilled:
        logs.ok("fingerprint", f"存量账号补配独立设备指纹 ×{backfilled}")
    monitor.start()
    captcha_manager.start()   # 验证码预解池后台补充
    _run_install_sequence_on_start()
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
