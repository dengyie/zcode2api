"""鉴权依赖：后台管理密钥 + 可选的网关 API Key。"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Query, status

from .store import store


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def verify_admin_key(
    authorization: str | None = Header(default=None),
    app_key: str | None = Query(default=None),
) -> None:
    """校验后台管理密钥。

    支持 `Authorization: Bearer <key>` 头或 `?app_key=<key>` 查询参数
    （后者用于 EventSource 等无法发送自定义头的场景）。
    """
    key = store.admin_key()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未配置后台密钥")

    token = _extract_bearer(authorization) or app_key
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少鉴权凭证")
    if not hmac.compare_digest(token, key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "鉴权凭证无效")


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
