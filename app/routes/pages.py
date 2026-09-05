"""页面路由：登录、账号管理、设置。

前端文件来自独立部署目录（settings.FRONTEND_DIR，前后端分离）——
改前端只需 rsync 该目录，无需重启后端。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import settings

router = APIRouter()

_TOKEN = "{{APP_VERSION}}"


def _html(name: str) -> HTMLResponse:
    path = settings.FRONTEND_DIR / "admin" / name
    if not path.exists():
        raise HTTPException(404, "页面不存在")
    body = path.read_text(encoding="utf-8").replace(_TOKEN, settings.frontend_version())
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


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
