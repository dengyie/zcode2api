"""全量 review 回归：删号写回、纯 Key 风控、设置脱敏。"""

from __future__ import annotations

from app.models import Account, Status
from app.store import Store


class TestDeletedAccountNotResurrected:
    def test_update_account_refuses_deleted_id(self, fresh_app):
        acc = fresh_app.add_account("zai", "a", "jwt.token.a")
        aid = acc.id
        assert fresh_app.remove_account("zai", aid)
        assert fresh_app.find("zai", aid) is None

        acc.status = Status.EXHAUSTED
        acc.last_error = "后台任务写回"
        fresh_app.update_account(acc)

        assert fresh_app.find("zai", aid) is None
        reloaded = Store()
        assert reloaded.find("zai", aid) is None
        assert [a.id for a in reloaded.list_accounts("zai")] == []


class TestPureApiKeyRiskBan:
    def test_pure_apikey_not_selectable_after_risk_ban(self):
        acc = Account.create("zai", "t", "sk-pure-api-key")
        assert acc.mode == "apiKey"
        assert acc.has_apikey_fallback() is False
        acc.ban_for_risk()
        assert acc.status == Status.DISABLED
        assert acc.enabled is True
        assert acc.is_selectable() is False

    def test_jwt_with_key_still_selectable_after_risk_ban(self):
        acc = Account.create("zai", "t", "a.b.c")
        acc.api_key = "sk-fallback"
        acc.ban_for_risk()
        assert acc.has_apikey_fallback() is True
        assert acc.is_selectable() is True
        assert acc.allows_billing() is False

    def test_pure_apikey_invalid_not_selectable(self):
        acc = Account.create("zai", "t", "sk-dead-key")
        acc.status = Status.INVALID
        assert acc.is_selectable() is False
