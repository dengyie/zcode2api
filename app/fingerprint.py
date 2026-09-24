"""每账号客户端指纹（设备档案）—— 入池生成高可信桌面 SKU，一号一台设备。

默认不再克隆部署机。hub 跑在 Linux 云主机上，把 N 个账号都标成同一套
云内核 + 1920x1080，会在上游聚成「一台机房机器开了 N 个 ZCode」。官方
客户端是 Electron 桌面（darwin-arm64 / win32-x64 为主），账号身份必须
长得像用户电脑。

生成规则：
  - 从成套 SKU 表抽样（platform × arch × os_version × screen 绑定），
    禁止字段笛卡尔积（darwin-arm64 + 1366x768 这类假电脑）。
  - 账号池不含 linux：官方桌面主形态是 Mac / Windows。
  - 语言/时区取真实地区对；device_mid 每次全新 UUIDv4，跨账号不复用。
  - 同账号档案一经分配即稳定；rotate() 换一整台 SKU（含新 MID）。

host_profile 仍采集部署机，只给诊断/测试；不作为入池默认源。
存量非 SKU 档案（旧版宿主机克隆）由启动回填换成生成 SKU。
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field, replace

# 成套桌面 SKU：(weight, platform, arch, os_version, screen)
# 分辨率只取官方桌面端常见值，并与平台绑定（Mac 逻辑分辨率不配 Windows）。
_SKUS: tuple[tuple[int, str, str, str, str], ...] = (
    # Apple silicon MacBook Air/Pro 13–14"（darwin 24 = Sequoia，25 = Tahoe）
    (10, "darwin", "arm64", "24.5.0", "1512x982"),
    (10, "darwin", "arm64", "24.6.0", "1512x982"),
    (8, "darwin", "arm64", "24.5.0", "1728x1117"),
    (8, "darwin", "arm64", "24.6.0", "1728x1117"),
    (8, "darwin", "arm64", "25.5.0", "1512x982"),
    (6, "darwin", "arm64", "25.5.0", "1728x1117"),
    (5, "darwin", "arm64", "23.6.0", "1512x982"),
    (4, "darwin", "arm64", "23.6.0", "1728x1117"),
    (4, "darwin", "arm64", "24.5.0", "2560x1440"),
    (3, "darwin", "arm64", "24.6.0", "2560x1600"),
    (2, "darwin", "arm64", "25.5.0", "2560x1440"),
    (2, "darwin", "arm64", "24.5.0", "3840x2160"),
    # Intel Mac 存量（Ventura/Sonoma；darwin 24+ 不再配 x64）
    (2, "darwin", "x64", "23.6.0", "1920x1080"),
    (2, "darwin", "x64", "22.6.0", "1440x900"),
    (1, "darwin", "x64", "23.6.0", "2560x1440"),
    # Windows 11 主流 + 少量 Win10
    (8, "win32", "x64", "10.0.22631", "1920x1080"),
    (7, "win32", "x64", "10.0.26100", "1920x1080"),
    (5, "win32", "x64", "10.0.22631", "2560x1440"),
    (4, "win32", "x64", "10.0.26200", "1920x1080"),
    (3, "win32", "x64", "10.0.26100", "2560x1440"),
    (3, "win32", "x64", "10.0.22621", "1920x1080"),
    (2, "win32", "x64", "10.0.22631", "3840x2160"),
    (2, "win32", "x64", "10.0.19045", "1920x1080"),
    (1, "win32", "x64", "10.0.19045", "1366x768"),
    (1, "win32", "x64", "10.0.26100", "2560x1600"),
    (1, "win32", "x64", "10.0.22000", "1920x1080"),
)
_SKU_POOL: tuple[tuple[str, str, str, str], ...] = tuple(
    (platform, arch, os_version, screen)
    for weight, platform, arch, os_version, screen in _SKUS
    for _ in range(weight)
)
_SKU_COMBOS = frozenset(_SKU_POOL)

# host_real 校验仍认这些平台取值（部署机可能是 linux）
_HOST_PLATFORMS = ("darwin", "win32", "linux")
_HOST_ARCHS = ("arm64", "x64")
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

_SCREEN_RE = re.compile(r"^\d{3,4}x\d{3,4}$")
# os.release() 形态门（host_real 放行用）：主版本.次版本.修订 + 可选后缀
_RELEASE_SHAPE = re.compile(r"^\d+\.\d+(\.\d+)?[\w.\-]*$")


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


def sku_combos() -> frozenset[tuple[str, str, str, str]]:
    """生成器允许的 (platform, arch, os_version, screen) 成套组合。"""
    return _SKU_COMBOS


def is_generated_sku(profile: DeviceProfile) -> bool:
    """档案是否为入池用的高可信桌面 SKU（旧版宿主机克隆为 False）。"""
    return (profile.platform, profile.arch, profile.os_version, profile.screen) in _SKU_COMBOS


def _validate(profile: DeviceProfile, host_real: bool = False) -> None:
    """合规校验：档案内部自洽（成套 SKU / 真机形态、地区对、分辨率、UUID）。

    host_real=True 只用于宿主机采集：平台按取值形态放行（linux/arm64 云主机
    等真实形态），os_version 放宽为内核版本形态。账号生成路径走严格 SKU 门。
    """
    if host_real:
        if profile.platform not in _HOST_PLATFORMS or profile.arch not in _HOST_ARCHS:
            raise ValueError(f"非法平台形态: {profile.platform_full}")
        if profile.os_version not in _OS_VERSIONS.get(profile.platform, ()):
            if not _RELEASE_SHAPE.match(profile.os_version):
                raise ValueError(f"os_version 与平台不符: {profile.platform}/{profile.os_version}")
    else:
        if not is_generated_sku(profile):
            raise ValueError(
                f"非桌面 SKU: {profile.platform_full}/{profile.os_version}/{profile.screen}"
            )
        if (profile.language, profile.timezone) not in _LOCALES:
            raise ValueError(f"语言/时区组合不真实: {profile.language}/{profile.timezone}")
    if not _SCREEN_RE.match(profile.screen):
        raise ValueError(f"分辨率形态非法: {profile.screen}")
    uuid.UUID(profile.device_mid)  # 必须是合法 UUID


def random_profile() -> DeviceProfile:
    """随机生成一份成套桌面 SKU 档案（生成时自校验）。"""
    platform, arch, os_version, screen = secrets.choice(_SKU_POOL)
    language, timezone = secrets.choice(_LOCALES)
    profile = DeviceProfile(
        platform=platform,
        arch=arch,
        os_version=os_version,
        language=language,
        timezone=timezone,
        screen=screen,
        device_mid=str(uuid.uuid4()),
    )
    _validate(profile)
    return profile


def profile_for(account) -> DeviceProfile:
    """取账号档案：无则生成分配（幂等）。仅内存态分配，落库由调用方 save。"""
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


def host_profile(device_mid: str | None = None):
    """宿主机真实档案（诊断/测试用，不是入池默认源）。

    device_mid 缺省 = DeviceProfile 缺省工厂的全新 UUID；传入
    quota.device_mid() 即「这台机器」语义。
    """
    from . import hostinfo

    profile = hostinfo.collect_host_profile()
    if device_mid:
        profile = replace(profile, device_mid=device_mid)
    _validate(profile, host_real=True)
    return profile


def assign(account) -> DeviceProfile:
    """入池分配档案：成套高可信桌面 SKU + 全新 device_mid（一号一台设备）。"""
    account.fingerprint = random_profile()
    return account.fingerprint


def rotate(account) -> DeviceProfile:
    """换发全新档案（device_mid 必变；风控后换设备语义）。

    换发换一整台成套 SKU（平台/内核/屏幕都可能变）+ 新 device_mid。
    """
    account.fingerprint = random_profile()
    return account.fingerprint
