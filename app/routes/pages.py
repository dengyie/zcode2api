"""页面路由：登录、账号管理、设置。

前后端分离（2026-11-14）：后端只做**纯静态文件服务**（FileResponse，etag/
last-modified 协商缓存），不做任何服务端模板替换；前端资源可整体 rsync 到
任意静态源，配合 ZCODE_CORS_ORIGINS 即可跨源调用本 API。业务数据一律走
/admin/api/*（JSON + Bearer），与页面完全解耦。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from .. import settings

router = APIRouter()


def _html(name: str) -> FileResponse:
    path = settings.FRONTEND_DIR / "admin" / name
    if not path.exists():
        raise HTTPException(404, "页面不存在")
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin")


@router.get("/admin", include_in_schema=False)
async def admin_root():
    return RedirectResponse("/admin/login")


@router.get("/admin/login", include_in_schema=False)
async def admin_login():
    return _html("login.html")


@router.get("/admin/accounts", include_in_schema=False)
async def admin_accounts():
    return _html("accounts.html")


@router.get("/admin/monitoring", include_in_schema=False)
async def admin_monitoring():
    return _html("monitoring.html")


@router.get("/admin/settings", include_in_schema=False)
async def admin_settings():
    return _html("settings.html")


@router.get("/meta", include_in_schema=False)
async def meta():
    return {"version": settings.APP_VERSION}
