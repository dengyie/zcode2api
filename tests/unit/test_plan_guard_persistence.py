"""plan_guard 冷却持久化（2.7.55）：SQLite 落盘 + 启动回读。

2026-11-17 夜间线上实证：supervisord / HA 看门狗 / 部署重叠期 9 次重启
（含 2 次 SIGKILL），纯内存冷却表每次清零——重启后第一个 GLM-5.3 请求把
全池 rem=0 的号挨个撞一遍 1005（reqlog 实锤 35 次打点、拦截 0 次）。
"""
import sqlite3
import time

import pytest

from app import plan_guard, settings


@pytest.fixture(autouse=True)
def _fresh_guard(tmp_path, monkeypatch):
    """独立 data 目录 + 清空模块内存态，用例间互不污染。"""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_DIR", data_dir)
    monkeypatch.setattr(plan_guard, "_plan_model_exhausted", {})
    monkeypatch.setattr(plan_guard, "_loaded", False)
    # 清掉线程局部连接（上一用例的旧库文件连接不能带过来）
    if hasattr(plan_guard._local, "conn"):
        plan_guard._local.conn = None
    yield
    conn = getattr(plan_guard._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        # 关闭后必须置回 None：同进程后续集成测试（conftest 换了 DATA_DIR）
        # 的 _connect() 若拿到这个已关闭连接，sqlite3.Error 会被吞掉，
        # note() 静默不落盘——2.7.56 全量回归实锤的测试间污染。
        plan_guard._local.conn = None


class TestPersistence:
    def test_note_persists_to_sqlite(self, tmp_path):
        """note() 即写盘：db 文件出现且含该条目。"""
        plan_guard.note("acc-1", "GLM-5.3", time.time() + 1800)
        db = tmp_path / "data" / "plan_guard.db"
        assert db.exists()
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT key, until FROM plan_model_exhausted"
            ).fetchall()
        assert len(rows) == 1
        key, until = rows[0]
        assert "acc-1" in key and "GLM-5.3" in key

    def test_reload_after_restart(self, tmp_path):
        """重启模拟：内存态清空 → 重新回读 → 冷却仍生效（核心修复语义）。"""
        plan_guard.note("acc-2", "GLM-5.3", time.time() + 1800)
        assert plan_guard.cooling("acc-2", "GLM-5.3")

        # 重启：内存态清零 + loaded 标志复位（fixture 同款手法）
        plan_guard._plan_model_exhausted.clear()
        plan_guard._loaded = False

        assert plan_guard.cooling("acc-2", "GLM-5.3")  # 盘上条目救回

    def test_reload_skips_expired(self, tmp_path):
        """回读只取未过期条目：过期冷却重启后不拦（自然重探）。"""
        plan_guard.note("acc-3", "GLM-5.3", time.time() - 10)  # 已过期
        plan_guard._plan_model_exhausted.clear()
        plan_guard._loaded = False

        assert not plan_guard.cooling("acc-3", "GLM-5.3")

    def test_clear_model_persists(self, tmp_path):
        """billing 定向清除同步删盘：重启后不复活。"""
        plan_guard.note("acc-4", "GLM-5.3", time.time() + 1800)
        plan_guard.clear_model("acc-4", "GLM-5.3")

        plan_guard._plan_model_exhausted.clear()
        plan_guard._loaded = False
        assert not plan_guard.cooling("acc-4", "GLM-5.3")

    def test_clear_account_persists(self, tmp_path):
        """整号清除同步删盘（人工启用 = 立即重试，重启后不复活）。"""
        plan_guard.note("acc-5", "GLM-5.3", time.time() + 1800)
        plan_guard.note("acc-5", "GLM-5.3-Flash", time.time() + 1800)
        plan_guard.note("acc-6", "GLM-5.3", time.time() + 1800)
        plan_guard.clear_account("acc-5")

        plan_guard._plan_model_exhausted.clear()
        plan_guard._loaded = False
        assert not plan_guard.cooling("acc-5", "GLM-5.3")
        assert not plan_guard.cooling("acc-5", "GLM-5.3-Flash")
        assert plan_guard.cooling("acc-6", "GLM-5.3")  # 别的号不连坐

    def test_cooldown_ttl_boundary_survives_restart(self, tmp_path):
        """TTL 到期边界：重启不延长冷却——已过期的条目回读即失活。
        （负偏移打点，免 sleep：全量回归下 0.3s sleep 会因负载抖动假阴。）"""
        plan_guard.note("acc-7", "GLM-5.3", time.time() - 10)  # 已过期
        plan_guard._plan_model_exhausted.clear()
        plan_guard._loaded = False
        assert not plan_guard.cooling("acc-7", "GLM-5.3")


class TestDiskHygiene:
    """2.7.56 磁盘卫生：DB 行只进不出会无界累积——启动清理 + 到期同步删盘。"""

    def _db_rows(self, tmp_path):
        db = tmp_path / "data" / "plan_guard.db"
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT key FROM plan_model_exhausted"
            ).fetchall()

    def test_startup_prune_deletes_expired_rows(self, tmp_path):
        """启动回读顺带物理清除过期行：磁盘不随历史耗尽事件无界累积。"""
        plan_guard.note("acc-old", "GLM-5.3", time.time() - 10)  # 过期行
        plan_guard.note("acc-live", "GLM-5.3", time.time() + 1800)
        plan_guard._plan_model_exhausted.clear()
        plan_guard._loaded = False

        # 任意 cooling() 触发一次性回读 + 启动清理
        assert plan_guard.cooling("acc-live", "GLM-5.3")

        keys = {k for (k,) in self._db_rows(tmp_path)}
        assert all("acc-old" not in k for k in keys)  # 过期行已被物理删除
        assert any("acc-live" in k for k in keys)  # 未过期行保留

    def test_cooling_expiry_deletes_db_row(self, tmp_path):
        """cooling() 到期摘除同步删盘（显式 now 参数，免 sleep）。"""
        plan_guard.note("acc-8", "GLM-5.3", time.time() + 1800)
        assert plan_guard.cooling("acc-8", "GLM-5.3")

        # now 越过 until：到期分支 pop 内存 + _persist(None) 删盘
        assert not plan_guard.cooling("acc-8", "GLM-5.3", now=time.time() + 2000)
        assert not plan_guard.cooling("acc-8", "GLM-5.3")  # 内存已摘
        assert self._db_rows(tmp_path) == []  # 盘已删


class TestConcurrencyContract:
    """2.7.57 并发契约：note 与到期摘除锁内互斥 + 到期重检查防误删。"""

    def _db_rows(self, tmp_path):
        db = tmp_path / "data" / "plan_guard.db"
        with sqlite3.connect(db) as conn:
            return conn.execute(
                "SELECT key, until FROM plan_model_exhausted"
            ).fetchall()

    def test_renote_wins_over_stale_expiry(self, tmp_path):
        """交错语义钉：note() 已覆盖更晚 until 时，到期摘除不得洗掉新鲜冷却。

        模拟竞态：cooling 先读到旧 until，note 插入更晚解禁，cooling 继续
        执行到期摘除——锁内重检查发现键已被覆盖，必须维持冷却（内存+盘）。"""
        fresh_until = time.time() + 3600
        plan_guard.note("acc-r", "GLM-5.3", time.time() + 60)  # 旧冷却
        k = plan_guard._key("acc-r", "GLM-5.3")
        old_until = plan_guard._plan_model_exhausted[k]

        # 模拟 note 抢先覆盖（真实交错中发生在 cooling 读后、摘除前）
        plan_guard._plan_model_exhausted[k] = fresh_until
        plan_guard._persist(k, fresh_until)

        # cooling 持旧 until 进入到期判定（显式 now 越过旧冷却）
        assert plan_guard.cooling("acc-r", "GLM-5.3", now=old_until + 30)
        assert plan_guard._plan_model_exhausted[k] == fresh_until  # 内存保留
        assert (k, fresh_until) in self._db_rows(tmp_path)  # 盘保留新鲜值

    def test_warm_loads_and_prunes(self, tmp_path):
        """lifespan 预热 warm()：显式回读未过期 + 物理清除过期，语义同惰性路径。"""
        plan_guard.note("acc-w", "GLM-5.3", time.time() + 1800)
        plan_guard.note("acc-exp", "GLM-5.3", time.time() - 10)
        plan_guard._plan_model_exhausted.clear()
        plan_guard._loaded = False

        plan_guard.warm()
        assert plan_guard.cooling("acc-w", "GLM-5.3")  # 未过期条目已入内存
        keys = {k for (k, _) in self._db_rows(tmp_path)}
        assert all("acc-exp" not in k for k in keys)  # 过期行已物理清除
        # 二次 warm 幂等（_loaded 已置位，直接返回）
        plan_guard.warm()
        assert plan_guard.cooling("acc-w", "GLM-5.3")
