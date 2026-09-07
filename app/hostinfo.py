"""宿主机真实指纹采集 —— 默认指纹源（「模仿真机安装」模式）。

用户决策（2026-09-07）：默认**不再随机编造**设备档案，而是采集部署机的真实
平台数据，让 hub 在上游视角就是「装在这台机器上的一份真 ZCode」。随机池
（fingerprint.random_profile）降级为兜底/显式轮转选项。

采集项 ↔ DeviceProfile 字段：
  platform    platform.system()   → darwin / win32 / linux（官方 process.platform 语义）
  arch        platform.machine()  → arm64 / x64（官方 process.arch 语义）
  os_version  platform.release()  → os.release() 同源，官方 X-Os-Version 直接用它
  timezone    /etc/localtime → IANA 名：符号链接反解；实体文件则与
              /usr/share/zoneinfo 字节比对；都失败退 UTC
  language    $LANG（zh_CN.UTF-8 → zh-CN；缺失退 en-US）
  screen      本机无显示器（服务器形态）→ 官方桌面端必有屏幕，取 HOST_FALLBACK
  device_mid  不在本模块生成 —— 复用 quota.device_mid() 的「首装生成、持久化、
              永久复用」语义，与官方客户端 telemetry deviceMid 完全一致

所有值仍过 fingerprint._validate 合规门：真机数据天然合规（Linux 内核版本
不在预置池时，_validate 按「版本形态」放行本机采集值，见 fingerprint 备注）。
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path

# 服务器无显示器时的兜底分辨率（官方桌面端激活事件必有 screen_resolution）
FALLBACK_SCREEN = "1920x1080"

_TZ_LANG_RE = re.compile(r"^([a-z]{2,3})(?:[_-]([A-Za-z]{2,4}))?")


def _resolve_timezone() -> str:
    """IANA 时区名：官方客户端读系统时区（Intl.resolvedOptions().timeZone）。

    解析顺序：
      1. /etc/localtime 符号链接路径反解（darwin 通常如此）
      2. /etc/localtime 为实体文件时（部分 Linux 发行版是拷贝而非链接，
         如 pxed），与 /usr/share/zoneinfo 逐文件字节比对取唯一匹配
      3. 都失败退 UTC
    """
    path = Path("/etc/localtime")
    try:
        target = os.path.realpath(path)
        parts = Path(target).parts
        for i, seg in enumerate(parts):
            if seg == "zoneinfo" and i + 2 < len(parts):
                return f"{parts[i + 1]}/{parts[i + 2]}"
    except OSError:
        pass
    try:
        content = path.read_bytes()
    except OSError:
        return "UTC"
    zoneinfo_dir = Path("/usr/share/zoneinfo")
    if not zoneinfo_dir.is_dir():
        return "UTC"
    candidates: list[str] = []
    for f in zoneinfo_dir.rglob("*"):
        if not f.is_file() or f.suffix or f.is_symlink():
            continue  # 二进制 TZif 无后缀；跳过链接（posix/RIGHT 别名）与非数据文件
        rel = f.relative_to(zoneinfo_dir).as_posix()
        if rel.startswith(("Etc/", "posix/", "right/")) or rel in ("posixrules", "leapseconds"):
            continue
        if not rel[0].isupper():
            continue  # 真实地名以大写开头；纯缩写文件（CST 等）不作候选
        try:
            if f.read_bytes() == content:
                candidates.append(rel)
        except OSError:
            continue
    if not candidates:
        return "UTC"
    # Area/City 两段式优先于单段，字典序稳定输出
    candidates.sort(key=lambda r: (0 if "/" in r else 1, r))
    return candidates[0]


def _resolve_language() -> str:
    """语言标签：$LANG 的 zh_CN.UTF-8 形态转 zh-CN；无 Locale 环境退 en-US。

    官方桌面端 locale 来自系统偏好；Linux 容器常为 C/C.UTF-8（无语言信息），
    此时与官方常见默认 en-US 对齐（真实安装于裸 Locale 主机时同样如此）。
    """
    raw = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").strip()
    m = _TZ_LANG_RE.match(raw)
    if not m:
        return "en-US"
    lang, region = m.group(1), m.group(2)
    if not region:
        return {"zh": "zh-CN", "en": "en-US", "ja": "ja-JP", "ko": "ko-KR",
                "de": "de-DE", "fr": "fr-FR"}.get(lang, "en-US")
    return f"{lang}-{region.upper()}"


def _normalize_arch(machine: str) -> str:
    # process.arch 语义：arm64 / x64（官方取值）；其余按 64 位推断为 x64
    m = (machine or "").lower()
    if "arm" in m or "aarch" in m:
        return "arm64"
    return "x64"


def _normalize_platform(system: str) -> str:
    return {"darwin": "darwin", "windows": "win32", "linux": "linux"}.get(
        (system or "").lower(), "linux")


def host_profile_cls():
    """延迟导入 DeviceProfile（避免与 fingerprint 循环依赖）。"""
    from .fingerprint import DeviceProfile
    return DeviceProfile


def collect_host_profile():
    """采集本机真实设备档案（每次调用重采，落库由调用方负责）。"""
    DeviceProfile = host_profile_cls()
    return DeviceProfile(
        platform=_normalize_platform(platform.system()),
        arch=_normalize_arch(platform.machine()),
        os_version=platform.release() or "0.0",
        language=_resolve_language(),
        timezone=_resolve_timezone(),
        screen=FALLBACK_SCREEN,
    )
