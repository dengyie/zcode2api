"""上游身份 uid 级去重（2.7.41）：

- jwt_upstream_uid：user_id/sub/id claim 解析 + 畸形容错
- add_account：同 uid 新签 JWT 回写旧行（不建新行）——2026-11-15 生产事故
  （trial/briar 同 uid 各 2 行双节点并行，一号双 IP 风控暴露面）
- 启动 _merge_duplicate_identities：重复行合并，保留新签 enabled 行，
  旧行删除不再占用互斥节点集合
"""

import base64
import json

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
