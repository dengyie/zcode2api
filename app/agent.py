"""上游请求构建。

负责根据账号凭证选择端点、组装请求头。实际发送与流式透传在 routes/gateway.py。
"""

from __future__ import annotations

from . import constants, settings
from .models import Account

# 透传客户端 header 时需要剔除的字段
_DROP_HEADERS = {
    "host",
    "content-length",
    "x-api-key",
    "authorization",
    "user-agent",
    "http-referer",
    "accept-encoding",
    "connection",
}


def build_request(
    account: Account,
    body: dict,
    verify_param: str | None,
    incoming_headers: dict | None = None,
) -> tuple[str, dict]:
    """返回 (目标 URL, 请求头)。"""
    provider = account.provider

    if provider == "zai":
        if account.mode == "jwt" and account.jwt_token:
            target_url = settings.UPSTREAM["zai"]
            auth = {"Authorization": f"Bearer {account.jwt_token}"}
        elif account.api_key:
            target_url = settings.UPSTREAM["zai_fallback"]
            auth = {"x-api-key": account.api_key}
        else:
            raise RuntimeError("账号缺少有效凭证")
    elif provider == "bigmodel":
        target_url = settings.UPSTREAM["bigmodel"]
        if not account.api_key:
            raise RuntimeError("BigModel 账号缺少 API Key")
        auth = {"x-api-key": account.api_key}
    else:
        raise RuntimeError(f"未知提供商: {provider}")

    headers = {
        "content-type": "application/json",
        **auth,
        "anthropic-version": constants.ANTHROPIC_VERSION,
        "User-Agent": settings.USER_AGENT,
        "X-ZCode-App-Version": constants.X_ZCODE_APP_VERSION,
        "X-ZCode-Agent": constants.X_ZCODE_AGENT,
        "HTTP-Referer": constants.HTTP_REFERER,
    }
    if verify_param:
        headers[constants.CAPTCHA_HEADER] = verify_param

    for key, value in (incoming_headers or {}).items():
        lower = key.lower()
        if lower in _DROP_HEADERS or lower.startswith("x-zcode"):
            continue
        headers[key] = value

    return target_url, headers
