"""每账号客户端指纹（设备档案）—— 随机生成合规档案。

为什么需要：全池共用一套 device_mid + os/locale 指纹时，多账号在上游视角
是「同一台设备上的多个身份」——这是显式关联信号。此处为每个账号生成一份
独立、内部自洽的设备档案（UUIDv4 device_mid + 平台/版本/语言/时区/分辨率），
随账号持久化（Account.fingerprint 字段，store 全量落库），删除账号即档案作废。

随机生成（random_profile）而非固定模板，合规 = 官方客户端真实会出现的组合：
  - X-Platform  = {platform}-{arch}，仅取真实主流组合
    （darwin×arm64/x64、win32×x64、linux×x64；win-arm64 桌面占有率可忽略）
  - X-Os-Version = os.release() 语义，按平台从各自版本池取（darwin 2x.x 内核
    ↔ macOS 13–26；win32 = 10.0.{build}；linux = 发行版内核包版本）
  - 语言/时区取真实地区对（zh-CN↔上海、en-US↔纽约/洛杉矶、ja-JP↔东京…）
  - 分辨率取桌面端常见值；device_mid 每次全新 UUIDv4，跨账号永不复用

assign（入池分配）与 rotate（风控后换发）都以 random_profile 为源：换发必得
全新档案（device_mid 必变）。同账号档案一经分配即稳定幂等，不像爬虫乱跳。
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field

# 平台×架构：官方 process.platform-process.arch 的真实主流组合
_PLATFORM_ARCHS = (
    ("darwin", "arm64"),
    ("darwin", "x64"),
    ("win32", "x64"),
    ("linux", "x64"),
)
# os.release() 语义版本池（按平台）。darwin 2x.x ↔ macOS 13–26 内核；
# win32 = 10.0.{build}（19045=Win10 22H2 … 26200=Win11 25H2）；linux = 内核包版本。
_OS_VERSIONS = {
    "darwin": ("22.6.0", "23.6.0", "24.5.0", "24.6.0", "25.5.0"),
    "win32": ("10.0.19045", "10.0.22000", "10.0.22621", "10.0.22631", "10.0.26100", "10.0.26200"),
    "linux": ("5.15.0-91-generic", "6.1.0-18-amd64", "6.8.0-45-generic"),
}
# 语言-时区真实地区组合（X-Client-Language ↔ X-Client-Timezone，激活事件同源）
_LOCALES = (
    ("zh-CN", "Asia/Shanghai"),
    ("en-US", "America/New_York"),
    ("en-US", "America/Los_Angeles"),
    ("en-GB", "Europe/London"),
    ("de-DE", "Europe/Berlin"),
    ("ja-JP", "Asia/Tokyo"),
    ("ko-KR", "Asia/Seoul"),
    ("en-SG", "Asia/Singapore"),
)
# 桌面端常见分辨率（激活事件 screen_resolution）
_SCREENS = (
    "1920x1080", "2560x1440", "3840x2160", "5120x2880",
    "2560x1600", "1728x1117", "1512x982", "1440x900", "1366x768",
)

_SCREEN_RE = re.compile(r"^\d{3,4}x\d{3,4}$")


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


def _validate(profile: DeviceProfile) -> None:
    """合规校验：档案内部自洽（平台↔版本↔分类、地区对、分辨率、UUID）。"""
    if (profile.platform, profile.arch) not in _PLATFORM_ARCHS:
        raise ValueError(f"非法平台组合: {profile.platform_full}")
    if profile.os_version not in _OS_VERSIONS.get(profile.platform, ()):
        raise ValueError(f"os_version 与平台不符: {profile.platform}/{profile.os_version}")
    if (profile.language, profile.timezone) not in _LOCALES:
        raise ValueError(f"语言/时区组合不真实: {profile.language}/{profile.timezone}")
    if not _SCREEN_RE.match(profile.screen):
        raise ValueError(f"分辨率形态非法: {profile.screen}")
    uuid.UUID(profile.device_mid)  # 必须是合法 UUID


def random_profile() -> DeviceProfile:
    """随机生成一份合规设备档案（生成时自校验）。"""
    platform, arch = secrets.choice(_PLATFORM_ARCHS)
    language, timezone = secrets.choice(_LOCALES)
    profile = DeviceProfile(
        platform=platform,
        arch=arch,
        os_version=secrets.choice(_OS_VERSIONS[platform]),
        language=language,
        timezone=timezone,
        screen=secrets.choice(_SCREENS),
        device_mid=str(uuid.uuid4()),
    )
    _validate(profile)
    return profile


def profile_for(account) -> DeviceProfile:
    """取账号档案：无则随机分配（幂等）。仅内存态分配，落库由调用方 save。"""
    fp = getattr(account, "fingerprint", None)
    if isinstance(fp, DeviceProfile):
        return fp
    if isinstance(fp, dict) and fp.get("device_mid"):
        profile = DeviceProfile(
            platform=fp["platform"], arch=fp["arch"], os_version=fp["os_version"],
            language=fp["language"], timezone=fp["timezone"], screen=fp["screen"],
            device_mid=fp["device_mid"],
        )
        account.fingerprint = profile
        return profile
    return assign(account)


def assign(account) -> DeviceProfile:
    """为账号随机分配一份新档案（device_mid 全新）。"""
    account.fingerprint = random_profile()
    return account.fingerprint


def rotate(account) -> DeviceProfile:
    """换发全新随机档案（device_mid 必变；风控后换设备语义）。"""
    account.fingerprint = random_profile()
    return account.fingerprint
