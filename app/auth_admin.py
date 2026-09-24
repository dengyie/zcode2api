"""鉴权依赖：后台管理密钥 + 可选的网关 API Key。"""

from __future__ import annotations

import hmac
import time

from fastapi import Header, HTTPException, Query, Request, status

from .store import store

ADMIN_FAIL_LIMIT = 8
ADMIN_LOCK_SECONDS = 300

# ip -> {count, locked_until}
_failures: dict[str, dict] = {}


def reset_failures() -> None:
    """测试夹具：清空登录失败计数。"""
    _failures.clear()


def _client_ip(request: Request) -> str:
    for header in ("cf-connecting-ip", "x-real-ip"):
        val = (request.headers.get(header) or "").strip()
        if val:
            return val.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _is_locked(ip: str) -> bool:
    rec = _failures.get(ip)
    if not rec:
        return False
    until = float(rec.get("locked_until") or 0)
    if until and time.time() < until:
        return True
    if until and time.time() >= until:
        _failures.pop(ip, None)
    return False


def _record_failure(ip: str) -> None:
    rec = _failures.setdefault(ip, {"count": 0, "locked_until": 0.0})
    rec["count"] = int(rec.get("count") or 0) + 1
    if rec["count"] >= ADMIN_FAIL_LIMIT:
        rec["locked_until"] = time.time() + ADMIN_LOCK_SECONDS


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def verify_admin_key(
    request: Request,
    authorization: str | None = Header(default=None),
    app_key: str | None = Query(default=None),
) -> None:
    """校验后台管理密钥。

    支持 `Authorization: Bearer <key>` 头或 `?app_key=<key>` 查询参数
    （后者用于 EventSource 等无法发送自定义头的场景）。
    同一客户端连续失败达到上限后临时锁死，正确密码也要等锁过期。
    """
    ip = _client_ip(request)
    if _is_locked(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "登录失败次数过多，请稍后再试")

    key = store.admin_key()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未配置后台密钥")

    token = _extract_bearer(authorization) or app_key
    if token is None:
        _record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少鉴权凭证")
    if not hmac.compare_digest(token, key):
        _record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "鉴权凭证无效")
    _failures.pop(ip, None)


async def verify_gateway_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """校验 /v1/messages 网关访问密钥（未配置则放行）。"""
    key = store.gateway_key()
    if not key:
        return
    token = _extract_bearer(authorization) or x_api_key
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 API Key")
    if not hmac.compare_digest(token, key):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API Key 无效")
