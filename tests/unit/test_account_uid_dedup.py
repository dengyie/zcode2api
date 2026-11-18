"""上游身份 uid 级去重（2.7.41）：

- jwt_upstream_uid：user_id/sub/id claim 解析 + 畸形容错
- add_account：同 uid 新签 JWT 回写旧行（不建新行）——2026-11-15 生产事故
  （trial/briar 同 uid 各 2 行双节点并行，一号双 IP 风控暴露面）
- 启动 _merge_duplicate_identities：重复行合并，保留新签 enabled 行，
  旧行删除不再占用互斥节点集合
"""

import base64
import json

import pytest

from app.models import jwt_upstream_uid
from app.store import Store


def _jwt(uid: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"user_id": uid}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig"


class TestJwtUpstreamUid:
    def test_extracts_user_id(self):
        assert jwt_upstream_uid(_jwt("u-123")) == "u-123"

    def test_non_jwt_returns_none(self):
        assert jwt_upstream_uid("plain-api-key") is None
        assert jwt_upstream_uid(None) is None

    def test_malformed_payload_returns_none(self):
        assert jwt_upstream_uid("a.b.c") is None  # 载荷非 base64 JSON

    def test_sub_only_not_treated_as_identity(self):
        """只认 user_id：sub/id 是通用 JWT 语义，不同账号的测试桩
        （同 sub 异 token）不得被误合并——2026-11-15 回归教训。"""
        payload = base64.urlsafe_b64encode(json.dumps({"sub": "x"}).encode()).decode().rstrip("=")
        assert jwt_upstream_uid(f"h.{payload}.sig") is None


class TestAddAccountUidDedup:
    def test_same_uid_new_token_merges_into_existing_row(self, fresh_app: Store):
        """同号重登 JWT 变化：不建新行，回写旧行保 id/绑定/指纹。"""
        first = fresh_app.add_account("zai", "reg", _jwt("uid-A"))
        first.egress_node = "node-keep"
        fresh_app.update_account(first)

        second = fresh_app.add_account("zai", "reg", _jwt("uid-A").replace(".sig", ".sig2"))
        assert second.id == first.id  # 同一行
        assert len(fresh_app.list_accounts("zai")) == 1
        assert second.jwt_token == _jwt("uid-A").replace(".sig", ".sig2")  # token 已更新为重登新签
        assert second.egress_node == "node-keep"  # 绑定不丢
        assert second.status == "active"

    def test_different_uid_creates_new_row(self, fresh_app: Store):
        fresh_app.add_account("zai", "reg", _jwt("uid-A"))
        fresh_app.add_account("zai", "reg2", _jwt("uid-B"))
        assert len(fresh_app.list_accounts("zai")) == 2


class TestStartupMergeDuplicates:
    def test_merge_keeps_newest_enabled_row(self, tmp_path, monkeypatch):
        """启动合并：同 uid 两行 → 保留 created_at 新者，旧行删除。"""

        from app.models import Account

        monkeypatch.setattr("app.settings.DB_PATH", tmp_path / "accounts.db")

        # 真实 Store 隔离库：种两行同 uid 重复数据（模拟历史 bug 遗留）后重载
        seed = Store()
        old = Account(id="dup-old", name="dup", provider="zai", mode="jwt",
                      jwt_token=_jwt("uid-DUP"), created_at=1000.0,
                      egress_node="node-stale", use_count=5, fail_count=1)
        new = Account(id="dup-new", name="dup", provider="zai", mode="jwt",
                      jwt_token=_jwt("uid-DUP") + "x", created_at=2000.0,
                      egress_node="node-fresh", use_count=2)
        seed._accounts["zai"] = [old, new]
        seed._persist_account(old)
        seed._persist_account(new)

        store = Store()  # 重新 _load → 触发 _merge_duplicate_identities
        rows = store.list_accounts("zai")
        assert [a.id for a in rows] == ["dup-new"]  # 旧行被合并删除
        kept = rows[0]
        assert kept.egress_node == "node-fresh"  # 保留行的绑定不动
        assert kept.use_count == 7 and kept.fail_count == 1  # 计数并集

    def test_no_uid_rows_untouched(self, fresh_app: Store):
        """apiKey / 畸形 JWT 不参与合并（无上游身份可比）。"""
        fresh_app.add_account("zai", "k1", "plain-key")
        fresh_app.add_account("zai", "k2", "plain-key2")
        fresh_app._merge_duplicate_identities()
        assert len(fresh_app.list_accounts("zai")) == 2


class TestEditPathUidGate:
    @pytest.mark.asyncio
    async def test_edit_rejects_foreign_uid_token(self, fresh_app, gateway_client):
        """编辑路径 uid 闸：把 B 号的重登 token 灌进 A 号 → 409 拒绝
        （add_account 已收口，admin PUT 是最后一个绕过口，2026-11-15）。"""
        client, _ = gateway_client
        a = fresh_app.add_account("zai", "acc-a", _jwt("uid-A"))
        fresh_app.add_account("zai", "acc-b", _jwt("uid-B"))

        res = await client.put(
            f"/admin/api/accounts/{a.id}", json={"token": _jwt("uid-B") + "y"},
            headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 409
        assert "acc-b" in res.json()["detail"]
        # A 号 token 未被污染
        assert fresh_app.find_any(a.id).jwt_token == _jwt("uid-A")
        assert len(fresh_app.list_accounts("zai")) == 2  # 不产生第三行

    @pytest.mark.asyncio
    async def test_edit_own_uid_token_still_allowed(self, fresh_app, gateway_client):
        """同号重登 token 原地更新自己的行——正常操作不拦。"""
        client, _ = gateway_client
        a = fresh_app.add_account("zai", "acc-a", _jwt("uid-A"))
        new_token = _jwt("uid-A") + "z"
        res = await client.put(
            f"/admin/api/accounts/{a.id}", json={"token": new_token},
            headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        assert fresh_app.find_any(a.id).jwt_token == new_token


class TestEditGateAtomicity:
    @pytest.mark.asyncio
    async def test_edit_409_does_not_leak_name_change(self, fresh_app, gateway_client):
        """R5：409 拒绝不得留下半生效的 name 变更（闸在任何写入之前）。"""
        client, _ = gateway_client
        a = fresh_app.add_account("zai", "acc-a", _jwt("uid-A"))
        fresh_app.add_account("zai", "acc-b", _jwt("uid-B"))

        res = await client.put(
            f"/admin/api/accounts/{a.id}",
            json={"name": "renamed-a", "token": _jwt("uid-B") + "y"},
            headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 409
        assert fresh_app.find_any(a.id).name == "acc-a"  # name 未漂移


class TestProbeStreakLifecycle:
    @pytest.mark.asyncio
    async def test_rebalance_success_clears_streak(self, fresh_app, monkeypatch):
        """R1：换绑提交即清 streak——旧节点失败账不得背到新节点头上。
        （否则新节点首轮抖动即够 2/2 误换绑。）"""
        from app import egress_pool as egress_pool_mod
        from app import settings as _settings

        # 本测试验证 TP rebalance 语义，显式进 tetherpool 供给
        # （2.7.68 起 listeners 供给被供给模式 gate 拦截，另有专测）
        monkeypatch.setattr(_settings, "PER_ACCOUNT_EGRESS", True)
        monkeypatch.setattr(_settings, "EGRESS_SUPPLY", "tetherpool")

        pool = egress_pool_mod.egress_pool
        acc = fresh_app.add_account("zai", "streak-acc", _jwt("uid-STREAK"))
        acc.enabled = True
        acc.egress_node = "node-old"
        pool._probe_fail_streak[acc.id] = 1  # 旧节点失败一轮的残留

        async def fake_tp_ban(*a, **k):
            return True

        async def fake_tp_acquire(account, force_rebalance=False, extra_exclude=None):
            account.egress_node = "node-new"
            return {"node_name": "node-new", "exit_ip": "1.2.3.4"}

        async def fake_probe(account, proxy_url, jwt):
            return 200, '{"data":{"model":"GLM"}}'  # JSON-APP 放行

        monkeypatch.setattr(egress_pool_mod, "tp_ban", fake_tp_ban)
        monkeypatch.setattr(egress_pool_mod, "tp_acquire", fake_tp_acquire)
        monkeypatch.setattr(pool, "_probe_via", fake_probe)
        monkeypatch.setattr(
            "app.captcha.captcha_manager.invalidate", lambda *a, **k: None)

        ok = await pool.rebalance(acc, reason="health_probe_fail", ban=False)
        assert ok is True
        assert acc.id not in pool._probe_fail_streak  # 已清零
