"""账号与设置的持久化存储（SQLite）。

数据保存在项目本目录下的 data/accounts.db，采用 WAL 模式，
与 grok2api 的本地 (local) 账号后端保持一致。

运行期账号对象常驻内存（保证轮询游标与状态实时性），
每次变更同步落库；进程启动时从 SQLite 读取快照。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing

from . import logs, settings
from .models import PROVIDERS, Account, Status

_TBL = "accounts"
_META = "meta"


def _parse_hours(raw: str) -> list[int]:
    """解析领取窗口（北京时间小时，逗号分隔；空/* = 全天 → []）。升序去重，脏值跳过。"""
    raw = str(raw or "").strip()
    if raw in ("", "*"):
        return []
    hours: list[int] = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        try:
            hour = int(part)
        except ValueError:
            continue  # 脏值跳过，不让一个坏配置挂掉领取轮
        if 0 <= hour <= 23 and hour not in hours:
            hours.append(hour)
    return sorted(hours)


# 已告警过的坏配置值（解析发生在每次循环唤醒/设置读取，去重避免刷屏）
_HOURS_WARNED: set[str] = set()


class Store:
    """线程安全的账号 / 设置存储，含轮询游标。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accounts: dict[str, list[Account]] = {p: [] for p in PROVIDERS}
        self._settings: dict = {}
        self._rotation: dict[str, int] = {p: 0 for p in PROVIDERS}
        self._init_db()
        self._load()

    # ── SQLite 基础 ──────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {_META} (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {_TBL} (
                    id          TEXT PRIMARY KEY,
                    provider    TEXT NOT NULL,
                    name        TEXT,
                    mode        TEXT,
                    status      TEXT,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL,
                    data        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_acc_provider ON {_TBL} (provider);
                CREATE INDEX IF NOT EXISTS idx_acc_status   ON {_TBL} (status);
                """
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('admin_key', ?)",
                (settings.DEFAULT_ADMIN_KEY,),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('gateway_key', '')"
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('quota_refresh_interval', ?)",
                (str(settings.QUOTA_REFRESH_INTERVAL),),
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {_META} (key, value) VALUES ('claim_round_interval', ?)",
                (str(settings.CLAIM_ROUND_INTERVAL),),
            )
            conn.commit()

    def _load(self) -> None:
        with closing(self._connect()) as conn:
            meta_rows = conn.execute(f"SELECT key, value FROM {_META}").fetchall()
            self._settings = {r["key"]: r["value"] for r in meta_rows}
            self._settings.setdefault("admin_key", settings.DEFAULT_ADMIN_KEY)
            self._settings.setdefault("gateway_key", "")
            self._settings.setdefault("quota_refresh_interval", str(settings.QUOTA_REFRESH_INTERVAL))
            self._settings.setdefault("claim_round_interval", str(settings.CLAIM_ROUND_INTERVAL))

            self._accounts = {p: [] for p in PROVIDERS}
            rows = conn.execute(
                f"SELECT data FROM {_TBL} ORDER BY created_at ASC"
            ).fetchall()
            for row in rows:
                try:
                    account = Account.from_dict(json.loads(row["data"]))
                except (json.JSONDecodeError, TypeError):
                    continue
                if account.provider in self._accounts:
                    self._accounts[account.provider].append(account)

    def _persist_account(self, account: Account) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                f"""INSERT OR REPLACE INTO {_TBL}
                    (id, provider, name, mode, status, enabled, created_at, data)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (
                    account.id, account.provider, account.name, account.mode,
                    account.status, 1 if account.enabled else 0, account.created_at,
                    json.dumps(account.to_dict(), ensure_ascii=False),
                ),
            )
            conn.commit()

    def _delete_account(self, account_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(f"DELETE FROM {_TBL} WHERE id = ?", (account_id,))
            conn.commit()

    def _set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO {_META} (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def save(self) -> None:
        """全量落库（兜底接口）。"""
        with self._lock:
            for accounts in self._accounts.values():
                for account in accounts:
                    self._persist_account(account)

    # ── 设置 ─────────────────────────────────────────────────────────────────
    def get_setting(self, key: str, default=None):
        with self._lock:
            return self._settings.get(key, default)

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self._settings[key] = str(value)
            self._set_meta(key, str(value))

    def admin_key(self) -> str:
        return str(self.get_setting("admin_key", settings.DEFAULT_ADMIN_KEY) or "")

    def gateway_key(self) -> str:
        return str(self.get_setting("gateway_key", "") or "")

    def quota_refresh_interval(self) -> int:
        try:
            return max(0, int(self.get_setting("quota_refresh_interval", settings.QUOTA_REFRESH_INTERVAL)))
        except (TypeError, ValueError):
            return settings.QUOTA_REFRESH_INTERVAL

    def account_concurrency(self) -> int:
        """单账号并发上限。写死 2（2026-11-04 线上实测）：env 与后台 meta 覆盖
        一律作废（历史 meta 值如 account_concurrency=4 自动失效）。"""
        return settings.ACCOUNT_CONCURRENCY

    def claim_round_interval(self) -> int:
        """套餐自动领取轮间隔（0 = 关闭）。运行时可改（meta 表），改后即生效。"""
        try:
            return max(0, int(self.get_setting("claim_round_interval", settings.CLAIM_ROUND_INTERVAL)))
        except (TypeError, ValueError):
            return settings.CLAIM_ROUND_INTERVAL

    def claim_round_hours(self) -> list[int]:
        """自动领取窗口：北京时间小时升序列表；空 = 全天（不限时）。运行时可改。

        上游套餐按天投放（用户侧实证：每天 19:00 放量），窗口外轮询只会空转
        preview。窗口锚定**北京时钟**而非 host 时区（host 换成 UTC 也不漂）；
        中国无夏令时，固定 +8 即精确。
        """
        raw = str(self.get_setting("claim_round_hours", settings.CLAIM_ROUND_HOURS) or "").strip()
        hours = _parse_hours(raw)
        if not hours and raw not in ("", "*"):
            # 配置写了但一个有效小时都没有（env 笔误 / 手改 DB——管理端 PUT 有校验
            # 不会产生这种值）：**不允许坏配置静默升级成「全天轮询」**，回落默认并留痕。
            fallback = _parse_hours(settings.CLAIM_ROUND_HOURS)
            if raw not in _HOURS_WARNED:
                _HOURS_WARNED.add(raw)
                logs.warn("store", f"claim_round_hours={raw!r} 无有效小时，按默认处理 → {fallback or '全天'}")
            return fallback
        return hours

    # ── 账号读取 ─────────────────────────────────────────────────────────────
    def list_accounts(self, provider: str | None = None) -> list[Account]:
        with self._lock:
            if provider:
                return list(self._accounts.get(provider, []))
            return [a for p in PROVIDERS for a in self._accounts[p]]

    def find(self, provider: str, id_or_name: str) -> Account | None:
        with self._lock:
            return self._find_locked(provider, id_or_name)

    def find_any(self, id_or_name: str) -> Account | None:
        with self._lock:
            for p in PROVIDERS:
                for a in self._accounts[p]:
                    if a.id == id_or_name:
                        return a
        return None

    def _find_locked(self, provider: str, id_or_name: str) -> Account | None:
        for a in self._accounts.get(provider, []):
            if a.id == id_or_name or a.name == id_or_name:
                return a
        return None

    # ── 账号增删改 ───────────────────────────────────────────────────────────
    def add_account(self, provider: str, name: str, secret: str) -> Account:
        if provider not in PROVIDERS:
            raise ValueError(f"不支持的 provider: {provider}")
        account = Account.create(provider, name, secret)
        with self._lock:
            for a in self._accounts[provider]:
                if a.secret and a.secret == account.secret:
                    return a  # 跳过重复 token
            self._assign_fingerprint(account)  # 入池即分配独立设备指纹
            account.install_id = str(uuid.uuid4())  # 安装身份：稳定安装令牌
            self._accounts[provider].append(account)
            self._persist_account(account)
        return account

    @staticmethod
    def _assign_fingerprint(account: Account) -> None:
        """给账号分配客户端指纹并固化为 dict（随 asdict 落库，取用时还原）。"""
        from .fingerprint import profile_for

        profile = profile_for(account)
        account.fingerprint = {
            "platform": profile.platform, "arch": profile.arch,
            "os_version": profile.os_version, "language": profile.language,
            "timezone": profile.timezone, "screen": profile.screen,
            "device_mid": profile.device_mid,
        }

    def remove_account(self, provider: str, id_or_name: str) -> bool:
        with self._lock:
            items = self._accounts.get(provider, [])
            target = next((a for a in items if a.id == id_or_name or a.name == id_or_name), None)
            if not target:
                return False
            self._accounts[provider] = [a for a in items if a.id != target.id]
            self._delete_account(target.id)
            return True

    def update_account(self, account: Account) -> bool:
        """持久化某个账号的当前状态。

        账号已从内存池删除时拒绝写回，避免后台任务 INSERT OR REPLACE 把已删行救活。
        """
        with self._lock:
            if self._find_locked(account.provider, account.id) is None:
                return False
            self._persist_account(account)
            return True

    def set_enabled(self, provider: str, id_or_name: str, enabled: bool) -> bool:
        with self._lock:
            account = self._find_locked(provider, id_or_name)
            if not account:
                return False
            account.enabled = enabled
            if not enabled:
                account.status = Status.DISABLED
            elif account.status in (Status.DISABLED, Status.COOLING):
                # 人工启用 = 确认恢复：禁用洗回 active；冷却中启用视为跳过等待
                #（风控退避本就是自动恢复语义，人工提前放行同样成立）
                account.status = Status.ACTIVE
                account.cooling_until = None
            self._persist_account(account)
            return True

    # ── 负载均衡与亲和选择 ───────────────────────────────────────────────────
    def select(
        self,
        provider: str,
        skip_ids: set[str] | None = None,
        inflight: dict[str, int] | None = None,
        pinned_id: str | None = None,
        max_inflight: int = settings.ACCOUNT_CONCURRENCY,
    ) -> Account | None:
        """选择下一个可用账号。

        策略：
        1. 亲和账号优先：若指定 pinned_id 且未满（inflight < max_inflight）且可用，直接选用。
        2. 负载均衡分配（新请求 或 亲和号满载/不可用时溢出）：
           - 过滤掉 is_selectable 为 False、在 skip_ids 中、或已达并发上限（inflight >= max_inflight）的账号；
           - 主排序：当前在飞数 inflight 升序（最小负载优先）；
           - 次排序：距离当前轮询指针的偏移量（保证在负载相同时均匀轮流分配到不同账号）；
           - 更新轮询指针为选中账号的下一位。
        """
        skip_ids = skip_ids or set()
        inflight_map = inflight or {}
        now = time.time()
        with self._lock:
            accounts = self._accounts.get(provider, [])
            # 1. 亲和号尝试
            if pinned_id and pinned_id not in skip_ids:
                for a in accounts:
                    if a.id == pinned_id:
                        if a.is_selectable(now) and inflight_map.get(a.id, 0) < max_inflight:
                            return a
                        break

            # 2. 候选池过滤
            pool = [
                a for a in accounts
                if a.is_selectable(now)
                and a.id not in skip_ids
                and inflight_map.get(a.id, 0) < max_inflight
            ]
            if not pool:
                return None

            cur_rot = self._rotation.get(provider, 0)
            n = len(pool)

            # 负载优先（最小在飞请求数），相同负载下按轮询偏移平局
            best_idx = 0
            best_tuple = (inflight_map.get(pool[0].id, 0), (0 - cur_rot) % n)
            for i in range(1, n):
                cur_tuple = (inflight_map.get(pool[i].id, 0), (i - cur_rot) % n)
                if cur_tuple < best_tuple:
                    best_tuple = cur_tuple
                    best_idx = i

            account = pool[best_idx]
            self._rotation[provider] = (best_idx + 1) % n
            return account

    # ── 导入 / 导出 ─────────────────────────────────────────────────────────
    def export(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "exported_at": time.time(),
                "providers": {
                    p: [
                        {"name": a.name, "mode": a.mode, "secret": a.secret}
                        for a in self._accounts[p]
                    ]
                    for p in PROVIDERS
                },
            }

    def import_accounts(self, payload: dict) -> int:
        providers = payload.get("providers", {})
        count = 0
        for provider, items in providers.items():
            if provider not in PROVIDERS or not isinstance(items, list):
                continue
            for it in items:
                secret = it.get("secret") or it.get("token") or it.get("jwtToken") or it.get("apiKey")
                if not secret:
                    continue
                self.add_account(provider, it.get("name", provider), secret)
                count += 1
        return count


# 单例
store = Store()
