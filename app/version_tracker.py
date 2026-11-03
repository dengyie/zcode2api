"""ZCode 官方版本自动感知与同步管理器（Dynamic Version Tracker）。

数据源：官方原生 Electron 更新清单接口（GET /api/v1/releases/electron/manifest）。
实证（2026-11-03 官方桌面端逆向）：
- 官方桌面端检查更新直连此端点；
- 返回 YAML 格式，包含最新版本（version）、发布日期（releaseDate）、
  官方原生 Markdown 更新日志（releaseNotes，含中英文双语）；
- 免鉴权公开只读端点。

工作机制：
- 启动时恢复历史缓存的最新版本（若开启自动同步，立即对齐伪装指纹）；
- 异步向上游拉取最新 electron manifest；
- 若 upstream version > 当前本地 version，且开启 auto_sync：
  动态调用 constants.set_active_version 对齐指纹，落库并记录日志；
- 定时轮询（默认每 12 小时），并在管理后台提供手动触发检查与日志展示。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass

import httpx
import yaml

from . import constants, logs, settings


def parse_semver(v: str) -> tuple[int, ...]:
    """提取版本数字用于大小比较，如 '3.17.2' -> (3, 17, 2)。"""
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums) if nums else (0,)


def is_newer_version(candidate: str, baseline: str) -> bool:
    """candidate 是否比 baseline 更新（纯数字分量比较）。"""
    c = parse_semver(candidate)
    b = parse_semver(baseline)
    max_len = max(len(c), len(b))
    c_pad = c + (0,) * (max_len - len(c))
    b_pad = b + (0,) * (max_len - len(b))
    return c_pad > b_pad


@dataclass
class VersionInfo:
    version: str
    release_date: str = ""
    release_name: str = ""
    release_notes: str = ""
    checked_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> VersionInfo:
        return cls(
            version=str(data.get("version") or ""),
            release_date=str(data.get("release_date") or ""),
            release_name=str(data.get("release_name") or ""),
            release_notes=str(data.get("release_notes") or ""),
            checked_at=float(data.get("checked_at") or 0.0),
        )


def parse_manifest(text: str) -> VersionInfo | None:
    """解析官方 electron manifest (YAML) 内容。"""
    if not text or not text.strip():
        return None
    try:
        data = yaml.safe_load(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ver_raw = data.get("version")
    ver = str(ver_raw or "").strip()
    if not ver:
        return None
    # YAML 1.1 陷阱：`version: 3.10` 会被 safe_load 解析成 float 3.1，str() 后
    # 版本号被截断（3.10 → "3.1"，比较时 (3,1) < (3,17,1) 永远判旧）。解析结果
    # 非字符串时回退用正则从原始文本提取字面量（2026-11-04 review 实证）。
    if not isinstance(ver_raw, str):
        m = re.search(r"^version:\s*['\"]?([0-9A-Za-z.\-+]+)", text, re.MULTILINE)
        if m:
            ver = m.group(1).strip()

    release_date = str(data.get("releaseDate") or "").strip()
    release_name = str(data.get("releaseName") or f"Release v{ver}").strip()

    # 优先抽取中文更新日志，其次 fallback 主 releaseNotes 或英文
    locales = data.get("releaseNotesByLocale") or {}
    notes = ""
    if isinstance(locales, dict):
        zh = locales.get("zh-CN") or {}
        if isinstance(zh, dict) and zh.get("markdown"):
            notes = str(zh["markdown"]).strip()
        elif isinstance(zh, str):
            notes = zh.strip()

    if not notes:
        rn = data.get("releaseNotes")
        if isinstance(rn, str) and rn.strip():
            notes = rn.strip()

    if not notes and isinstance(locales, dict):
        en = locales.get("en-US") or {}
        if isinstance(en, dict) and en.get("markdown"):
            notes = str(en["markdown"]).strip()

    return VersionInfo(
        version=ver,
        release_date=release_date,
        release_name=release_name,
        release_notes=notes,
        checked_at=time.time(),
    )


class VersionTracker:
    # 结果新鲜窗口（秒）：窗口内复用上次核验结果，避免领套餐前置核验、
    # 后台 12h 轮、管理后台手动核验三个触发源并发时重复打上游
    _FRESH_WINDOW = 300

    def __init__(self) -> None:
        self.latest_info: VersionInfo | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_result: dict | None = None
        self._last_checked: float = 0.0

    def init_from_store(self) -> None:
        """启动阶段从 store 恢复之前探测到的最新版本。"""
        from .store import store

        stored_ver = str(store.get_setting("zcode_latest_version", "") or "").strip()
        raw_info = str(store.get_setting("zcode_latest_info", "") or "").strip()
        if raw_info:
            try:
                self.latest_info = VersionInfo.from_dict(json.loads(raw_info))
            except Exception:
                pass

        auto_sync = store.get_setting(
            "auto_sync_client_version", str(settings.AUTO_SYNC_CLIENT_VERSION).lower()
        ) == "true"

        if auto_sync and stored_ver and is_newer_version(stored_ver, constants.CLIENT_APP_VERSION):
            constants.set_active_version(stored_ver)
            logs.info("version", f"恢复已同步的 ZCode 官方版本: v{stored_ver}")

    async def fetch_remote_manifest(self) -> VersionInfo | None:
        """向上游请求最新 manifest。

        trust_env=False 直连 + 官方同形 UA（2.7.16 review 修复）：自定义 UA
        （原 ZCode-Hub-Tracker）等于向上游 WAF 自报非官方代理身份；且 hub 进程
        env 代理（127.0.0.1:10822）2026-11-04 实测完全超时（15s HTTP 000），
        直连才是可用出口——与 claim/billing 链路同出口策略。
        """
        url = settings.ZCODE_MANIFEST_URL
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                res = await client.request(
                    "GET",
                    url,
                    headers={
                        "User-Agent": settings.USER_AGENT,
                        "Accept": "application/x-yaml,text/yaml,text/plain,*/*",
                    },
                )
                if res.status_code != 200:
                    logs.warn("version", f"查询官方版本 manifest 失败: HTTP {res.status_code}")
                    return None
                return parse_manifest(res.text)
        except Exception as err:
            logs.warn("version", f"查询官方版本 manifest 异常: {err}")
            return None

    def _fresh_result(self) -> dict | None:
        """新鲜窗口内的缓存结果；过期/从未核验返回 None。"""
        if self._last_result and time.time() - self._last_checked < self._FRESH_WINDOW:
            return self._last_result
        return None

    async def check_and_sync(self, *, force: bool = False) -> dict:
        """执行一次版本探测与同步。

        force=False 默认带新鲜度防抖（_FRESH_WINDOW 内直接复用上次结果）；
        force=True 跳过缓存强制拉取（管理后台「立即核验」语义）。并发调用
        由 asyncio.Lock 串行化，锁内二次校验新鲜度（double-checked）。
        """
        if not force:
            cached = self._fresh_result()
            if cached is not None:
                return cached
        async with self._lock:
            if not force:
                cached = self._fresh_result()
                if cached is not None:
                    return cached
            result = await self._check_once()
            self._last_result = result
            self._last_checked = time.time()
            return result

    async def _check_once(self) -> dict:
        """真正的探测与同步（调用方须持锁）。"""
        from .store import store

        info = await self.fetch_remote_manifest()
        if not info:
            current_ver = constants.CLIENT_APP_VERSION
            latest_ver = self.latest_info.version if self.latest_info else current_ver
            return {
                "ok": False,
                "current_version": current_ver,
                "latest_version": latest_ver,
                "has_update": is_newer_version(latest_ver, current_ver),
                "updated": False,
                "release_date": self.latest_info.release_date if self.latest_info else "",
                "release_name": self.latest_info.release_name if self.latest_info else "",
                "release_notes": self.latest_info.release_notes if self.latest_info else "",
                "checked_at": self.latest_info.checked_at if self.latest_info else 0.0,
            }

        self.latest_info = info
        store.set_setting("zcode_latest_version", info.version)
        store.set_setting("zcode_latest_info", json.dumps(info.to_dict(), ensure_ascii=False))

        current_ver = constants.CLIENT_APP_VERSION
        auto_sync = store.get_setting(
            "auto_sync_client_version", str(settings.AUTO_SYNC_CLIENT_VERSION).lower()
        ) == "true"
        has_new = is_newer_version(info.version, current_ver)
        updated = False

        if has_new:
            if auto_sync:
                constants.set_active_version(info.version)
                updated = True
                logs.ok("version", f"检测到 ZCode 官方发版 {info.version}（当前 {current_ver}），已自动对齐伪装指纹")
            else:
                logs.info("version", f"检测到 ZCode 官方新版 {info.version}（当前 {current_ver}，未开启自动对齐）")
        else:
            logs.info("version", f"ZCode 官方版本核验: 当前 v{current_ver}，最新 v{info.version}（已是对齐状态）")

        return {
            "ok": True,
            "current_version": constants.CLIENT_APP_VERSION,
            "latest_version": info.version,
            "has_update": has_new,
            "updated": updated,
            "release_date": info.release_date,
            "release_name": info.release_name,
            "release_notes": info.release_notes,
            "checked_at": info.checked_at,
        }

    async def _loop(self) -> None:
        # 启动后先等待 5 秒，避开其他模块初始启动流量
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except TimeoutError:
            pass

        while not self._stop.is_set():
            # 常驻任务必须自兜：单次探测异常（SQLite 抖动/持久化失败等）不得
            # 杀死轮询协程，否则自动感知静默失效直到重启（对齐 claim/quota 轮）
            try:
                await self.check_and_sync()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                logs.err("version", f"版本检测循环异常（下轮自动重试）: {err}")
            interval = max(60, settings.VERSION_CHECK_INTERVAL)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None


version_tracker = VersionTracker()
