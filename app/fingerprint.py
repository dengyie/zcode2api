"""每账号客户端指纹（设备档案）—— 默认宿主机真实数据，随机池作轮转/兜底。

用户决策（2026-09-07）：默认模式 =「模仿真机安装」——档案采自部署机真实平台
数据（hostinfo.collect_host_profile：真实 platform/arch/os_version/时区/语言），
device_mid 走官方 telemetry 语义（首装生成一次、持久化、永久复用）。hub 在上游
视角即「装在这台机器上的一份真 ZCode」，多账号共用本机身份是真实形态（一台
开发机上多个 ZCode 窗口/会话本就同设备）。

随机池（random_profile）保留两个用途：
  1. rotate（风控后换发）——显式换一台「虚拟设备」；
  2. 宿主机数据不合规时的兜底（如容器内缺失信息）。

合规 = 官方客户端真实会出现的组合：
  - X-Platform  = {platform}-{arch}，随机池仅取真实主流组合
    （darwin×arm64/x64、win32×x64、linux×x64；win-arm64 桌面占有率可忽略）；
    host_real 不受预置组合约束 —— 宿主机实际形态即真机事实（linux/arm64 等）
  - X-Os-Version = os.release() 语义，按平台从各自版本池取（darwin 2x.x 内核
    ↔ macOS 13–26；win32 = 10.0.{build}；linux = 发行版内核包版本）
    —— 例外：host_profile 采到的真实 Linux 内核版本（如 pxed 的 5.10.134-…）
    不在预置池，属真机事实，按形态校验后放行（见 _validate）。
  - 语言/时区取真实地区对（zh-CN↔上海、en-US↔纽约/洛杉矶、ja-JP↔东京…）
  - 分辨率取桌面端常见值；device_mid 每次全新 UUIDv4，跨账号永不复用

同账号档案一经分配即稳定幂等，不像爬虫乱跳。
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field, replace

from . import logs

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
# os.release() 形态门（host_real 放行用）：主版本.次版本.修订 + 可选后缀
# （Linux 内核打包后缀如 -generic / -amd64 / -18.0.11.lifsea8.x86_64）
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


def _validate(profile: DeviceProfile, host_real: bool = False) -> None:
    """合规校验：档案内部自洽（平台↔版本↔分类、地区对、分辨率、UUID）。

    host_real=True（宿主机真实档案）时按「真机事实优先于预置池」放宽两处：
    平台组合按取值形态放行（linux/arm64 云主机等真实形态，报别的反而是伪装），
    os_version 放宽为「内核版本形态」——真机事实（如 pxed 的
    5.10.134-18.0.11.lifsea8.x86_64）优先于预置池，但仍必须长得像
    os.release() 输出，防采集污染。随机池不受放宽，仍走严格组合门。
    """
    if host_real:
        if profile.platform not in ("darwin", "win32", "linux") or \
                profile.arch not in ("arm64", "x64"):
            raise ValueError(f"非法平台形态: {profile.platform_full}")
    elif (profile.platform, profile.arch) not in _PLATFORM_ARCHS:
        raise ValueError(f"非法平台组合: {profile.platform_full}")
    if profile.os_version not in _OS_VERSIONS.get(profile.platform, ()):
        if not (host_real and _RELEASE_SHAPE.match(profile.os_version)):
            raise ValueError(f"os_version 与平台不符: {profile.platform}/{profile.os_version}")
    if not host_real and (profile.language, profile.timezone) not in _LOCALES:
        # 随机档案必须取真实地区对；host_real 不做此约束 —— 真机上语言与时区
        # 独立配置（en-US locale + Asia/Shanghai 时区的开发机是真实存在形态），
        # 官方客户端两者分开读（Intl locale / timeZone），真实组合即合规。
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


def host_profile(device_mid: str | None = None):
    """宿主机真实档案（默认指纹源）。

    device_mid 缺省 = DeviceProfile 缺省工厂的全新 UUID（collect 每次重采，
    MID 归属由调用方定）：传入 quota.device_mid() 即「这台机器」语义，
    传入新 UUID 即「本机新装设备」语义（assign 的用法）。
    """
    from . import hostinfo

    profile = hostinfo.collect_host_profile()
    if device_mid:
        profile = replace(profile, device_mid=device_mid)
    _validate(profile, host_real=True)
    return profile


def assign(account) -> DeviceProfile:
    """入池分配档案：默认宿主机真实形态 + 全新 device_mid（每账号一台
    「本机新装设备」）。宿主机采集失败（异常平台/数据）时退随机池兜底 ——
    降级必须留痕（用户决策默认真机数据，静默失效等于功能丢失）。"""
    try:
        account.fingerprint = host_profile(device_mid=str(uuid.uuid4()))
    except (ValueError, OSError) as err:
        logs.warn("fingerprint", f"宿主机档案不合规，退随机池: {err}")
        account.fingerprint = random_profile()
    return account.fingerprint


def rotate(account) -> DeviceProfile:
    """换发全新档案（device_mid 必变；风控后换设备语义）。

    换发走随机池：宿主机形态不变时换发等于没换设备（上游按组合识别），
    全新随机档案才是「换了一台机器」。
    """
    account.fingerprint = random_profile()
    return account.fingerprint
