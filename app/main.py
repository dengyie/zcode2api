"""FastAPI 应用工厂 + 生命周期。"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import logs, plan_guard, reqlog, settings
from .captcha import captcha_manager
from .claim import claim_round
from .egress_pool import egress_pool  # 单例实例（module.start 不存在——start 是实例方法）
from .quota import monitor
from .routes import admin_api, gateway, pages
from .version_tracker import version_tracker


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles with `Cache-Control: no-cache` — etag revalidation on every
    request so deploys are visible immediately (CF/browser never serve stale)."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


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


def _backfill_fingerprints() -> list:
    """启动时给无指纹或非桌面 SKU（旧版宿主机克隆）的存量账号换发生成档案。

    换机后清 installed_at，由 lifespan 补跑按账号安装序（新 device_mid 需要
    自己的 app_launch / app_daily_active）。返回被替换的账号列表。
    """
    from .fingerprint import DeviceProfile, is_generated_sku, profile_for
    from .store import store

    replaced = []
    for account in store.list_accounts():
        fp = account.fingerprint
        needs = False
        if not isinstance(fp, dict) or not fp.get("device_mid"):
            needs = True
        else:
            try:
                profile = DeviceProfile(
                    platform=fp["platform"], arch=fp["arch"], os_version=fp["os_version"],
                    language=fp["language"], timezone=fp["timezone"], screen=fp["screen"],
                    device_mid=fp["device_mid"],
                )
            except (KeyError, TypeError, ValueError):
                needs = True
            else:
                needs = not is_generated_sku(profile)
        if needs:
            account.fingerprint = None
            account.installed_at = None
            profile_for(account)
            store._assign_fingerprint(account)
            store.update_account(account)
            replaced.append(account)
    return replaced


def _backfill_install_ids() -> int:
    """启动时给无 install_id 的存量账号补配安装身份（纯本地，无网络）并落库。"""
    import uuid as _uuid

    from .store import store

    backfilled = 0
    for account in store.list_accounts():
        if not (account.install_id or "").strip():
            account.install_id = str(_uuid.uuid4())
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
    if settings.REQLOG_PERSIST:
        restored = reqlog.restore()
        if restored:
            logs.ok("reqlog", f"请求监控日志恢复 ×{restored}（持久化开启）")
    replaced = _backfill_fingerprints()
    if replaced:
        logs.ok("fingerprint", f"存量账号补配独立设备指纹 ×{len(replaced)}")
        for acc in replaced:
            admin_api._schedule_install(acc)
    installed = _backfill_install_ids()
    if installed:
        logs.ok("install", f"存量账号补配安装身份 ×{installed}")
    plan_guard.warm()  # 冷却表回读+过期行清理前移到启动，调度路径不承担磁盘 I/O
    monitor.start()
    captcha_manager.start()   # 验证码预解池后台补充
    claim_round.start()       # 套餐自动领取轮（间隔 0 = 关闭）
    version_tracker.init_from_store()
    version_tracker.start()   # 官方版本自动探测与同步
    egress_pool.start()       # TetherPool 出口健康循环（supply=tetherpool 时生效）
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
        flushed = reqlog.shutdown_flush()
        if flushed:
            logs.info("reqlog", f"停机收口在途请求 ×{flushed}")
        logs.opslog_close()  # opslog 落盘句柄收口（防停机缓冲丢失）
        await monitor.stop()
        await claim_round.stop()
        await version_tracker.stop()
        await egress_pool.stop()
        await captcha_manager.close()


def create_app() -> FastAPI:
    app = FastAPI(title="zcode-hub", version=settings.APP_VERSION, lifespan=lifespan)

    # 前后端分离：前端默认同源 /static 服务，设置 ZCODE_CORS_ORIGINS 后即可
    # 部署到任意源跨域调用本 API（Bearer 鉴权，无 cookie，CSRF 面为零）。
    origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=86400,
        )



    app.include_router(pages.router)
    app.include_router(admin_api.router)
    app.include_router(gateway.router)
    app.mount("/static", RevalidatingStaticFiles(directory=str(settings.FRONTEND_DIR)), name="static")
    # 前端相对资产路径（js/... css/...）在 /admin/* 页面形态下的服务点；路由
    # （/admin/api/*、/admin/login 等）注册在前，优先级高于本挂载。
    # JS/CSS 等静态资产必须 no-cache（发版后 CF/浏览器立即 revalidate 新内容）：
    # StaticFiles 默认无 Cache-Control 头，CF heuristic 缓存会把旧 JS 边缘缓存数小时。
    app.mount("/admin", RevalidatingStaticFiles(directory=str(settings.FRONTEND_DIR / "admin"), html=True),
              name="admin-static")
    return app


app = create_app()
