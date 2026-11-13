"""鉴权依赖：后台管理密钥 + 可选的网关 API Key。"""

from __future__ import annotations

import hmac
import time

from fastapi import Header, HTTPException, Request, status

from . import settings
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


def _key_equal(token: str, key: str) -> bool:
    """常数时间比较（UTF-8 字节）：str 版 compare_digest 遇非 ASCII 会抛
    TypeError（500），等价于把「任意非 ASCII 输入」变成可观测的异常侧信道。"""
    return hmac.compare_digest(token.encode("utf-8"), key.encode("utf-8"))


async def verify_admin_key(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """校验后台管理密钥。

    只接受 `Authorization: Bearer <key>` 头。查询参数（`?app_key=`）已移除
    （2.7.33）：URL 会进访问日志、浏览器历史、Referer，密钥不应出现在 URL 里。
    同一客户端连续失败达到上限后临时锁死，正确密码也要等锁过期。
    """
    ip = _client_ip(request)
    if _is_locked(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "登录失败次数过多，请稍后再试")

    key = store.admin_key()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未配置后台密钥")

    token = _extract_bearer(authorization)
    if token is None:
        _record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少鉴权凭证")
    if not _key_equal(token, key):
        _record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "鉴权凭证无效")
    _failures.pop(ip, None)


async def verify_gateway_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """校验 /v1/messages 网关访问密钥（未配置则拒绝：fail-closed）。

    未配置密钥时放行等于把上游账号池暴露成公开代理——只要部署后忘了设置
    gateway key，任何人都能用你的额度。空密钥应当是「不可用」，而不是「无鉴权」
    （2.7.33）。后台首页会提示先设置网关密钥。
    """
    key = store.gateway_key()
    if not key:
        # 默认 fail-closed：未配置密钥时放行等于把上游账号池暴露成公开代理。
        # 仅显式设置 ZCODE_GATEWAY_ALLOW_NO_KEY=1（本地开发）才放行。
        if settings.GATEWAY_ALLOW_NO_KEY:
            return
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "网关密钥未配置：请先在后台设置 Gateway API Key 再使用 /v1 端点",
        )
    token = _extract_bearer(authorization) or x_api_key
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 API Key")
    if not _key_equal(token, key):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API Key 无效")
