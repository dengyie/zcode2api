"""Plan 通道模型级额度耗尽冷却（2.7.29）。

同一账号的不同模型额度独立（线上实证：GLM-5.3 旗舰上游 1005 exceed quota
limit 时同号 Flash 窗口仍有 2.9 亿）——点名单一模型的请求若不按模型记冷却，
会把全池每个号都撞一遍必然失败的 1005 才 503。

独立成模块的原因：gateway（打点/跳号）与 quota（billing 刷新主动解除）
两侧都要用，而 gateway → quota 已有依赖，quota 反向 import gateway 会成环
——与 app/gate.py「进程级状态独立模块」同一手法。

记忆形态：f"{account_id}\\n{model}" → 解禁 epoch。
- 打点：gateway exhaust 分支（1005/1113/1310，Plan 通道）
- 跳号：_dispatch 选号循环（不计 attempts，同满载跳过）
- 解除：① TTL 到期自然重探；② quota.fetch_quota 实查拿到该号账本时按模型
  定向清除（billing 知道**哪些窗口可用**——同号 Flash 有余量不等于旗舰恢复了，
  整号清除会把旗舰冷却反复抹掉，重探退化成 1 次/分钟）；③ 后台启用/凭证变更
  （admin set_enabled 调 clear_account 整号清除，人工启用 = 要求立即重试）

持久化（2.7.55）：此前是纯进程内存 dict，「重启即忘，重探一次代价可接受」
——2026-11-17 夜间线上实证该假设失守：supervisord / HA 看门狗 / 部署重叠期
9 次重启（含 2 次 SIGKILL），每次重启清零后第一个 GLM-5.3 请求就把全池
rem=0 的号挨个撞一遍 1005（reqlog 实锤 35 次打点、冷却拦截 0 次）。改落
SQLite（data/plan_guard.db，与 accounts.db 同目录）：打点/清除即写盘，启动
时回读（过期条目顺带清扫），重启不再失忆。2.7.56 补磁盘卫生（启动清理
过期行 + cooling 到期同步删盘），2.7.57 补并发契约与预热入口。

线程契约（2.7.57）：全部公开函数**只在事件循环线程同步调用**（现状：
gateway dispatch / quota 后台任务 / admin 路由均在同一 asyncio 线程）；
运行期读改用 threading.Lock 串行化（note 与 cooling 到期摘除的交错靠
到期重检查防误删新鲜冷却），移入线程池前必须先过一遍锁语义。
预热：main.lifespan 调 warm() 显式回读+清理，把首次回读与清理的磁盘
I/O 挪出请求调度路径；cooling() 的惰性触发保留为兜底（tests 不跑
lifespan 时依赖它）。
"""

from __future__ import annotations

import sqlite3
import threading
import time

from . import settings

# TTL 由 settings 注入（避免 settings ↔ 本模块的加载顺序问题），见
# settings.PLAN_MODEL_EXHAUST_COOLDOWN 的调用方传参。
_plan_model_exhausted: dict[str, float] = {}

# WAL 连接不能跨线程共享；按线程惰性建连（调用方多为事件循环线程，单连接）。
_local = threading.local()
_lock = threading.Lock()
_loaded = False


def _db_path():
    return settings.DATA_DIR / "plan_guard.db"


def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_db_path(), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plan_model_exhausted ("
            " key TEXT PRIMARY KEY, until REAL NOT NULL)"
        )
        _local.conn = conn
    return conn


def _ensure_loaded() -> None:
    """启动后首次访问时回读盘上未过期条目（一次性，惰性：随首次 cooling()
    触发，非进程启动即刻——warm() 显式预热走同一实现）。

    顺带把盘上已过期条目物理清除（2.7.56）：cooling() 到期只弹内存，
    DB 行只进不出会无界累积（每次 (账号，模型) 耗尽事件都留一行，进程
    长寿 + 多号多模型下按周累积）。启动清理 = 每进程生命周期一次，
    频率无忧。
    """
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            now = time.time()
            conn = _connect()  # 线程级长连接，不关闭（closing 会废掉缓存）
            rows = conn.execute(
                "SELECT key, until FROM plan_model_exhausted WHERE until > ?",
                (now,),
            ).fetchall()
            _plan_model_exhausted.update(dict(rows))
            # 过期行物理清除：磁盘卫生，不影响语义（过期条目本就不该再拦）
            conn.execute(
                "DELETE FROM plan_model_exhausted WHERE until <= ?", (now,)
            )
            conn.commit()
        except sqlite3.Error:
            pass  # 盘不可用退化为纯内存，与旧形态等价
        _loaded = True


def warm() -> None:
    """lifespan 启动预热：显式触发回读+清理，磁盘 I/O 挪出请求调度路径。

    2.7.56 的清理原为惰性触发（首次 cooling()），首次请求的候选循环可能
    撞上跨进程写锁 busy_timeout（最长 5s）；lifespan 阶段做掉，调度路径
    只剩内存判定。失败静默（盘不可用时惰性兜底仍在，语义不劣化）。
    """
    _ensure_loaded()


def _persist(key: str, until: float | None) -> None:
    """单条写盘（note=upsert / clear=delete）；盘失败不影响内存态。"""
    try:
        conn = _connect()  # 线程级长连接，不关闭（closing 会废掉缓存）
        if until is None:
            conn.execute("DELETE FROM plan_model_exhausted WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO plan_model_exhausted (key, until)"
                " VALUES (?, ?)",
                (key, until),
            )
        conn.commit()
    except sqlite3.Error:
        pass


def _key(account_id: str, model: str) -> str:
    return f"{account_id}\n{model}"


def note(account_id: str, model: str, until: float) -> None:
    """记录（账号，模型）额度耗尽冷却的解禁时刻。

    内存与盘写同锁串行（2.7.57）：与 cooling() 到期摘除互斥，杜绝
    「摘除删盘落在重新打点之后」把新鲜冷却洗掉的单侧失忆。表只有
    主键点查/点写（µs 级），持锁写盘不构成实际竞争面。
    """
    if not account_id or not model:
        return
    k = _key(account_id, model)
    with _lock:
        _plan_model_exhausted[k] = until
        _persist(k, until)


def cooling(account_id: str, model: str, now: float | None = None) -> bool:
    """该（账号，模型）组合是否仍在冷却中；到期自动摘除（自然重探）。

    到期分支持锁重检查（2.7.57）：锁内二次确认 until 未被 note() 覆盖
    才摘除——内存与盘在同一临界区内一起变，不会出现单侧失忆。
    """
    if not account_id or not model:
        return False
    _ensure_loaded()
    k = _key(account_id, model)
    with _lock:
        until = _plan_model_exhausted.get(k, 0)
        if not until:
            return False
        if (now or time.time()) < until:
            return True
        if _plan_model_exhausted.get(k, 0) != until:
            return True  # 读后已被重新打点（更晚解禁），继续冷却
        _plan_model_exhausted.pop(k, None)
        # 2.7.56：到期摘除同步删盘，否则 DB 行只进不出（同键下次 note 会
        # REPLACE，但废弃键永不清理）；锁内与内存摘除原子（2.7.57）。
        _persist(k, None)
        return False


def clear_model(account_id: str, model: str) -> None:
    """billing 实查确认该（账号，模型）有可用窗口时定向清除。

    粒度必须对齐打点：同号其它模型的冷却不连坐清除（它们是否耗尽仍未知，
    清了只会重演一次必然失败的 1005）。
    """
    k = _key(account_id, model)
    _plan_model_exhausted.pop(k, None)
    _persist(k, None)


def clear_account(account_id: str) -> None:
    """整号清除：后台人工启用（要求立即重试）/凭证变更时用。

    billing 刷新**不要**走这里——billing 只能证明「有窗口可用」的模型恢复了，
    证明不了其它模型没耗尽（2.7.30 review 整改）。
    """
    with _lock:  # 2026-11-22 review P3：补锁对齐 note/clear 自身锁纪律表述
        keys = [k for k in _plan_model_exhausted if k.split("\n", 1)[0] == account_id]
        for k in keys:
            _plan_model_exhausted.pop(k, None)
    for k in keys:
        _persist(k, None)
