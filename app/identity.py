"""上游身份头仿真 —— 对齐 zapi identity.ts 镜像的官方 ZCode 客户端 `pio` 头集合。

官方客户端对上游发的每个请求都携带这组 companion 头（指纹层），缺失或形状
不对都会提高 WAF 关注度。此处逐字段、按序复刻：

    HTTP-Referer, User-Agent, X-ZCode-App-Version, X-Title, X-ZCode-Agent,
    X-Platform, X-Release-Channel, X-Client-Language, X-Client-Timezone,
    X-Os-Category, X-Os-Version, X-Device-Mid

X-Device-Mid 复用 quota.device_mid()（UUIDv4，首次生成后持久化 data/device_mid，
与 billing 全家桶同一设备身份 —— 同机异 MID 本身就是异常信号）。

另含追踪头（zapi upstream.ts buildTraceHeaders）：coding-plan 通道发全 5 个
UUID/类型头。每请求重新生成 request-id / trace-id / query-id / session-id。
"""

from __future__ import annotations

import os
import platform
import re
import uuid

from . import constants, settings
from .quota import device_mid

# 打印可见 ASCII 门（ZCode bundle fio 助手）；任何头值不含此形态即丢弃该头
_ASCII_PRINTABLE = re.compile(r"^[\x20-\x7e]+$")


def _clean(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v if v and _ASCII_PRINTABLE.match(v) else None


def _os_category(sys_platform: str) -> str:
    if sys_platform == "darwin":
        return "macos"
    if sys_platform == "win32":
        return "windows"
    return "linux"


def _os_version() -> str:
    # darwin release() 是内核版本（25.5.0 = macOS 15.x），直接用作伪装值
    return platform.release()


def build_identity_headers() -> dict[str, str]:
    """构建完整身份头（保持 pio 的字段顺序；条件性缺失语义同样镜像）。"""
    app_version = _clean(constants.CLIENT_APP_VERSION)
    plat = _clean(os.getenv("ZCODE_IDENTITY_PLATFORM", platform.system().lower())) or "darwin"
    arch = _clean(os.getenv("ZCODE_IDENTITY_ARCH", platform.machine())) or "arm64"
    release = _clean(os.getenv("ZCODE_IDENTITY_RELEASE", _os_version()))
    channel = _clean(os.getenv("ZCODE_IDENTITY_RELEASE_CHANNEL", constants.IDENTITY_RELEASE_CHANNEL))
    language = _clean(os.getenv("ZCODE_IDENTITY_CLIENT_LANGUAGE", constants.IDENTITY_CLIENT_LANGUAGE))
    timezone = _clean(os.getenv("ZCODE_IDENTITY_CLIENT_TIMEZONE", constants.IDENTITY_CLIENT_TIMEZONE))
    device_mid_val = _clean(os.getenv("ZCODE_IDENTITY_DEVICE_MID", device_mid()))

    headers: dict[str, str] = {
        "HTTP-Referer": constants.HTTP_REFERER,
        "User-Agent": settings.USER_AGENT,
    }
    if app_version:
        headers["X-ZCode-App-Version"] = app_version
    headers["X-Title"] = constants.IDENTITY_TITLE
    headers["X-ZCode-Agent"] = constants.X_ZCODE_AGENT
    headers["X-Platform"] = f"{plat}-{arch}"
    if channel:
        headers["X-Release-Channel"] = channel
    if language:
        headers["X-Client-Language"] = language
    if timezone:
        headers["X-Client-Timezone"] = timezone
    if plat:
        headers["X-Os-Category"] = _os_category(plat)
    if release:
        headers["X-Os-Version"] = release
    if device_mid_val:
        headers["X-Device-Mid"] = device_mid_val
    return headers


def build_trace_headers() -> dict[str, str]:
    """coding-plan 追踪头：每请求全新 UUID（官方客户端行为）。

    start-plan 不发 x-query-id / x-session-id；本服务只跑 coding-plan 通道，
    因此全量下发（zapi upstream.ts buildTraceHeaders 非 start-plan 分支）。
    """
    return {
        "x-request-id": str(uuid.uuid4()),
        "x-zcode-session-type": "main",
        "x-zcode-trace-id": str(uuid.uuid4()),
        "x-query-id": str(uuid.uuid4()),
        "x-session-id": str(uuid.uuid4()),
    }
