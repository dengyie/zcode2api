"""admin /accounts 暴露 Key-1113 无余额冷却剩余秒数（2.7.45 可见性）。

冷却状态由 gateway 进程内字典持有（重启即忘），admin 只读映射展示，
不改调度逻辑。
"""
import base64
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store


def _jwt(uid: str = "uid-nb") -> str:
    """构造形如上游 JWT 的 token（HS256 形态，payload 带 user_id）。"""

    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"user_id": uid}).encode())
    sig = _b64(hmac.new(b"test-secret", f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _admin_client(fresh_app: Store) -> TestClient:
    """不跑 lifespan 的 TestClient：后台任务（claim_round 等）会绑定事件循环，
    with 形态退出时跨循环报 RuntimeError；这里只测 HTTP 映射，无需 lifespan。"""
    if not fresh_app.list_accounts():
        fresh_app.add_account("zai", "nb-cool", _jwt())
    fresh_app.set_setting("admin_key", "test-admin-key")
    app = create_app()
    return TestClient(
        app, headers={"Authorization": "Bearer test-admin-key"},
    )


def _account_id(fresh_app: Store) -> str:
    if not fresh_app.list_accounts():
        fresh_app.add_account("zai", "nb-cool", _jwt())
    return fresh_app.list_accounts()[0].id


def _account_view(fresh_app: Store, client: TestClient) -> dict:
    r = client.get("/admin/api/accounts")
    assert r.status_code == 200, r.text
    aid = _account_id(fresh_app)
    return next(a for a in r.json()["accounts"] if a["id"] == aid)


class TestKeyNoBalanceVisibility:
    def test_absent_by_default(self, fresh_app):
        client = _admin_client(fresh_app)
        assert _account_view(fresh_app, client)["key_no_balance_remaining_s"] == 0

    def test_cooling_reported(self, fresh_app):
        from app.routes import gateway

        aid = _account_id(fresh_app)
        gateway._key_no_balance[aid] = time.time() + 1234
        client = _admin_client(fresh_app)
        try:
            remaining = _account_view(fresh_app, client)["key_no_balance_remaining_s"]
            assert 1200 <= remaining <= 1234
        finally:
            gateway._key_no_balance.pop(aid, None)

    def test_expired_reports_zero(self, fresh_app):
        from app.routes import gateway

        aid = _account_id(fresh_app)
        gateway._key_no_balance[aid] = time.time() - 1
        client = _admin_client(fresh_app)
        try:
            assert _account_view(fresh_app, client)["key_no_balance_remaining_s"] == 0
        finally:
            gateway._key_no_balance.pop(aid, None)

    def test_remaining_decreases_over_time(self, fresh_app):
        """两次读数应随时间递减（冷却窗口真实流逝）。"""
        from app.routes import gateway

        aid = _account_id(fresh_app)
        gateway._key_no_balance[aid] = time.time() + 3600
        client = _admin_client(fresh_app)
        try:
            v1 = _account_view(fresh_app, client)["key_no_balance_remaining_s"]
            time.sleep(1.05)
            v2 = _account_view(fresh_app, client)["key_no_balance_remaining_s"]
            assert v2 < v1
        finally:
            gateway._key_no_balance.pop(aid, None)
