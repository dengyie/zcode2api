"""上游身份头仿真 —— 对齐 zapi identity.ts 镜像的官方 ZCode 客户端 `pio` 头集合。

官方客户端对上游发的每个请求都携带这组 companion 头（指纹层），缺失或形状
不对都会提高 WAF 关注度。此处逐字段、按序复刻：

    HTTP-Referer, User-Agent, X-ZCode-App-Version, X-Title, X-ZCode-Agent,
    X-Platform, X-Release-Channel, X-Client-Language, X-Client-Timezone,
    X-Os-Category, X-Os-Version, X-Device-Mid

X-Device-Mid 复用 quota.device_mid()（UUIDv4，首次生成后持久化 data/device_mid，
与 billing 全家桶同一设备身份 —— 同机异 MID 本身就是异常信号）。

另含追踪头（zapi upstream.ts buildTraceHeaders）：JWT 通道即 zapi 的
start-plan，只发 x-request-id / x-zcode-session-type / x-zcode-trace-id
三个头（start-plan 不发 x-query-id / x-session-id，误发触发 3012）。
每请求重新生成。
"""

from __future__ import annotations

import os
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
    # 兼容两种形态：platform.system() 产出 darwin/windows/linux，伪装平台用 win32
    if sys_platform in ("darwin", "macos"):
        return "macos"
    if sys_platform in ("win32", "windows"):
        return "windows"
    return "linux"


def build_identity_headers() -> dict[str, str]:
    """构建完整身份头（保持 pio 的字段顺序；条件性缺失语义同样镜像）。

    平台指纹默认固定伪装（constants.CLIENT_PLATFORM，与 zapi identity.ts 同策略）：
    服务端部署在 Linux 时 platform.* 会暴露云服务器特征（如阿里云 Lifsea 内核版本），
    与官方 ZCode 桌面端形状不符。env 覆盖仅用于指纹实验。
    """
    app_version = _clean(constants.CLIENT_APP_VERSION)
    _plat_arch = constants.CLIENT_PLATFORM.split("-")  # "darwin-arm64"
    plat = _clean(os.getenv("ZCODE_IDENTITY_PLATFORM", _plat_arch[0])) or "darwin"
    arch = _clean(os.getenv("ZCODE_IDENTITY_ARCH", _plat_arch[1] if len(_plat_arch) > 1 else "arm64")) or "arm64"
    release = _clean(os.getenv("ZCODE_IDENTITY_RELEASE", constants.IDENTITY_OS_VERSION))
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


def build_trace_headers(plan: str = "start-plan") -> dict[str, str]:
    """追踪头：每请求全新 UUID（对齐 zapi upstream.ts buildTraceHeaders）。

    通道差异（关键，误发会触发上游 3012 "unusual activity"）：
      - start-plan（JWT 通道，cred.jwt 存在）：只发 x-request-id /
        x-zcode-session-type / x-zcode-trace-id 三个头。**不发**
        x-query-id / x-session-id —— 官方客户端 start-plan 请求不带这两个。
      - coding-plan（API Key 通道）：额外发 x-query-id / x-session-id。

    本服务 JWT 通道即 zapi 的 start-plan，故默认 plan="start-plan"。
    """
    headers = {
        "x-request-id": str(uuid.uuid4()),
        "x-zcode-session-type": "main",
        "x-zcode-trace-id": str(uuid.uuid4()),
    }
    if plan != "start-plan":
        headers["x-query-id"] = str(uuid.uuid4())
        headers["x-session-id"] = str(uuid.uuid4())
    return headers
