"""每账号客户端指纹（设备档案）池。

为什么需要：全池共用一套 device_mid + os/locale 指纹时，多账号在上游视角
是「同一台设备上的多个身份」——这是显式关联信号。此处为每个账号分配一份
独立、内部自洽的设备档案（UUIDv4 device_mid + 平台/版本/语言/时区/分辨率），
随账号持久化（dataclass 字段，store 全量落库），删除账号即档案作废。

档案形态取自官方客户端可出现的真实组合：
  - X-Platform  = {platform}-{arch}（官方 TH() = process.platform-arch）
  - X-Os-Category = darwin→macos / win32→windows / linux→linux（_os_category 同规则）
  - X-Os-Version = os.release() 语义（darwin 25.x ≈ macOS 15/26 内核；win 为
    "10.0.x"；linux 为内核版本）
  - 语言/时区与平台无强绑定，但保持常见真实组合（zh-CN 或 en-US）

分配规则（assign）：账号已有档案则原样返回（幂等）；否则轮转取池中下一套
模板并生成全新 device_mid —— 保证相邻入池账号模板不同，device_mid 永不
跨账号复用。rotate：换发下一套模板 + 新 device_mid（风控后换设备语义）。
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeviceProfile:
    """单套客户端设备档案（所有值直接映射上游身份头/事件字段）。"""

    platform: str          # X-Platform 前半：darwin / win32 / linux
    arch: str              # arm64 / x64
    os_version: str        # X-Os-Version（os.release() 语义）
    language: str          # X-Client-Language
    timezone: str          # X-Client-Timezone（IANA）
    screen: str            # 激活事件 screen_resolution
    device_mid: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def platform_full(self) -> str:
        return f"{self.platform}-{self.arch}"

    @property
    def os_category(self) -> str:
        if self.platform in ("darwin", "macos"):
            return "macos"
        if self.platform in ("win32", "windows"):
            return "windows"
        return "linux"


# 指纹模板池：官方桌面端在用主流形态。device_mid 入池时才生成（每账号唯一）。
_TEMPLATES = (
    DeviceProfile("darwin", "arm64", "25.5.0", "zh-CN", "Asia/Shanghai", "2560x1440"),
    DeviceProfile("darwin", "x64", "23.6.0", "en-US", "America/Los_Angeles", "1920x1080"),
    DeviceProfile("win32", "x64", "10.0.22631", "zh-CN", "Asia/Shanghai", "3840x2160"),
    DeviceProfile("win32", "x64", "10.0.19045", "en-US", "America/New_York", "2560x1440"),
    DeviceProfile("linux", "x64", "6.8.0-45-generic", "en-US", "Europe/Berlin", "1920x1080"),
    DeviceProfile("darwin", "arm64", "24.5.0", "en-US", "Asia/Tokyo", "1728x1117"),
)

_rotator: itertools.cycle = itertools.cycle(_TEMPLATES)


def _next_template() -> DeviceProfile:
    """轮转取下一套模板（device_mid 丢弃模板默认值，由 assign 重生成）。"""
    tpl = next(_rotator)
    return DeviceProfile(
        platform=tpl.platform, arch=tpl.arch, os_version=tpl.os_version,
        language=tpl.language, timezone=tpl.timezone, screen=tpl.screen,
        device_mid=str(uuid.uuid4()),
    )


def profile_for(account) -> DeviceProfile:
    """取账号档案：无则分配（幂等）。仅内存态分配，落库由调用方 save。"""
    fp = getattr(account, "fingerprint", None)
    if isinstance(fp, DeviceProfile):
        return fp
    if isinstance(fp, dict) and fp.get("platform"):
        profile = DeviceProfile(**fp)
        account.fingerprint = profile
        return profile
    return assign(account)


def assign(account) -> DeviceProfile:
    """为新账号分配下一套模板 + 全新 device_mid。"""
    account.fingerprint = _next_template()
    return account.fingerprint  # type: ignore[return-value]


def rotate(account) -> DeviceProfile:
    """换发下一套模板 + 全新 device_mid（风控后换设备）。"""
    account.fingerprint = _next_template()
    return account.fingerprint  # type: ignore[return-value]
