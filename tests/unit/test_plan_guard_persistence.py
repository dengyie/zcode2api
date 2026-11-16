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
