"""账号安装身份（install_id）+ 按账号安装序测试（2026-09-13）。"""

from __future__ import annotations

import uuid

import httpx

from app.models import Account


def _make_acc() -> Account:
    return Account.create("zai", "t", "h1.eyJzdWIiOiJhIn0.sig")


# ── Account 字段与持久化 ─────────────────────────────────────────────────────
class TestInstallIdentityFields:
    def test_defaults(self):
        acc = _make_acc()
        assert acc.install_id is None
        assert acc.installed_at is None

    def test_old_row_without_fields_loads(self):
        acc = Account.from_dict({"id": "x", "name": "t", "provider": "zai",
                                 "mode": "jwt", "jwt_token": "a.b.c"})
        assert acc.install_id is None
        assert acc.installed_at is None

    def test_public_view_exposes_install_fields(self):
        acc = _make_acc()
        acc.install_id = str(uuid.uuid4())
        acc.installed_at = 123.0
        view = acc.public_view()
        assert view["install_id"] == acc.install_id
        assert view["installed_at"] == 123.0


class TestStoreBinding:
    def test_add_account_binds_install_id(self, fresh_app):
        acc = fresh_app.add_account("zai", "a", "jwt.token.a")
        assert acc.install_id
        uuid.UUID(acc.install_id)  # 合法 UUID
        assert acc.installed_at is None  # 安装序未跑，只完成绑定

    def test_install_ids_unique_across_accounts(self, fresh_app):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        b = fresh_app.add_account("zai", "b", "jwt.token.b")
        assert a.install_id != b.install_id

    def test_duplicate_secret_returns_existing_not_rebind(self, fresh_app):
        a = fresh_app.add_account("zai", "a", "jwt.token.same")
        again = fresh_app.add_account("zai", "b", "jwt.token.same")
        assert again.id == a.id
        assert again.install_id == a.install_id

    def test_persistence_roundtrip_keeps_install_id(self, fresh_app):
        from app.store import Store
        acc = fresh_app.add_account("zai", "a", "jwt.token.a")
        reloaded = Store()
        assert reloaded.find("zai", acc.id).install_id == acc.install_id

    def test_export_strips_install_fields(self, fresh_app):
        fresh_app.add_account("zai", "a", "jwt.token.a")
        payload = fresh_app.export()
        item = payload["providers"]["zai"][0]
        assert "install_id" not in item and "installed_at" not in item


async def test_startup_backfills_install_id(fresh_app, monkeypatch):
    """旧账号无 install_id：启动回填（纯本地，无网络请求）。"""
    import app.main as main_module

    acc = fresh_app.add_account("zai", "a", "jwt.token.a")
    acc.install_id = None
    fresh_app.update_account(acc)

    captured: list[str] = []

    def _fake_run_install_sequence():
        captured.append("net")  # 启动安装序若被触到即失败：回填必须纯本地
        return {"errors": []}

    monkeypatch.setattr(main_module, "_install_task", None)
    monkeypatch.setattr("app.install.run_install_sequence", _fake_run_install_sequence)
    main_module._backfill_install_ids()
    after = fresh_app.find("zai", acc.id)
    assert after.install_id
    uuid.UUID(after.install_id)
    assert captured == []


# ── 按账号安装序 ─────────────────────────────────────────────────────────────
class _PatchMock:
    """httpx.AsyncClient 注入 MockTransport，返回捕获列表（同 test_install 模式）。"""

    def __init__(self, monkeypatch, handler):
        captured: list[tuple[str, str, dict | None]] = []
        self.captured = captured

        def handler_wrapped(request: httpx.Request) -> httpx.Response:
            captured.append((request.method, request.url.path,
                             json.loads(request.content) if request.content else None))
            return handler(request)

        real = httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs.pop("transport", None)
            return real(transport=httpx.MockTransport(handler_wrapped),
                        timeout=kwargs.get("timeout", 5))

        monkeypatch.setattr(httpx, "AsyncClient", _patched)


import json  # noqa: E402  (供 _PatchMock 使用)


async def test_per_account_install_sequence_hits_upstream(fresh_app, monkeypatch):
    """按账号安装序：configs + app_launch/app_daily_active，用账号指纹与 user_id。"""
    from app import install
    from app.fingerprint import profile_for

    acc = fresh_app.add_account("zai", "t", "h1.eyJzdWIiOiJhIn0.sig")
    profile = profile_for(acc)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/client/configs"):
            return httpx.Response(200, json={"code": 0, "data": {"configs": {}}})
        return httpx.Response(200, json={"code": 0})

    _patch = _PatchMock(monkeypatch, handler)
    result = await install.run_install_sequence_for_account(acc)
    assert result["errors"] == []
    assert result["configs_fetched"] is True
    assert result["events_reported"] == ["app_launch", "app_daily_active"]
    assert result["installed"] is True
    assert result["skipped"] is False

    captured = _patch.captured
    assert len(captured) == 3  # configs + 2 events
    paths = [p for _, p, _ in captured]
    assert paths[0].endswith("/client/configs")
    assert sum("event/report" in p for p in paths) == 2
    events = [b for _, _, b in captured if b and "element_name" in b]
    assert [e["element_name"] for e in events] == ["app_launch", "app_daily_active"]
    assert events[0]["user_id"] == "a"          # JWT sub 兜底
    assert events[0]["device_mid"] == profile.device_mid


async def test_per_account_install_sequence_tolerates_failures(monkeypatch):
    """上游全挂：errors 留痕，不抛出，不落 installed_at。"""
    from app import install

    acc = _make_acc()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _PatchMock(monkeypatch, handler)
    result = await install.run_install_sequence_for_account(acc)
    assert len(result["errors"]) == 3  # configs + 2 events
    assert result["installed"] is False
    assert acc.installed_at is None


async def test_per_account_install_sequence_idempotent(monkeypatch):
    """installed_at 非空：直接跳过，零上游请求。"""
    from app import install

    acc = _make_acc()
    acc.installed_at = 123.0

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("installed 账号不得再打上游")

    _PatchMock(monkeypatch, handler)
    result = await install.run_install_sequence_for_account(acc)
    assert result["skipped"] is True
    assert result["installed"] is False


def _admin_client(fresh_app):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    return AsyncClient(transport=ASGITransport(app=create_app()),
                       base_url="http://admin.test")


async def test_admin_add_accounts_schedules_install(fresh_app, monkeypatch):
    """admin 批量加号：真新增触发按账号安装序；重复 token 不重复触发。"""
    from app.routes import admin_api as admin_module

    scheduled: list[str] = []

    async def _noop_refresh(accounts):
        return {"ok": 0, "fail": 0}

    def _fake_schedule(account):
        scheduled.append(account.name)

    monkeypatch.setattr(admin_module, "_schedule_install", _fake_schedule)
    monkeypatch.setattr(admin_module, "refresh_accounts", _noop_refresh)
    monkeypatch.setattr(admin_module, "_schedule_auto_claim", lambda acc: None)
    async with _admin_client(fresh_app) as client:
        res = await client.post("/admin/api/accounts",
                                json={"tokens": "jwt.token.a\njwt.token.a"},
                                headers={"Authorization": "Bearer zcode"})
    assert res.status_code == 200
    assert len(scheduled) == 1  # 去重后真新增只有 1 个（重复 token 不重复触发）


async def test_edit_jwt_keeps_existing_api_key(fresh_app):
    """编辑 JWT 不得清掉同账号已有 API Key 回退通道。"""
    acc = fresh_app.add_account("zai", "a", "old.jwt.tok")
    acc.api_key = "sk-keep-me"
    fresh_app.update_account(acc)
    async with _admin_client(fresh_app) as client:
        res = await client.put(
            f"/admin/api/accounts/{acc.id}",
            json={"token": "new.jwt.tok"},
            headers={"Authorization": "Bearer zcode"},
        )
    assert res.status_code == 200
    after = fresh_app.find("zai", acc.id)
    assert after.mode == "jwt"
    assert after.jwt_token == "new.jwt.tok"
    assert after.api_key == "sk-keep-me"


def test_cli_add_account_schedules_install(fresh_app, monkeypatch):
    """CLI 入池与 Web 入池一样要跑按账号安装序。"""
    import cli as cli_mod

    scheduled: list[str] = []

    async def _fake_install(account):
        scheduled.append(account.id)
        return {"installed": True, "skipped": False, "errors": []}

    async def _noop_claim(acc):
        return

    monkeypatch.setattr(cli_mod, "store", fresh_app)
    monkeypatch.setattr(cli_mod, "cli_auto_claim", _noop_claim)
    monkeypatch.setattr("app.install.run_install_sequence_for_account", _fake_install)
    cli_mod.cmd_add_account(["zai", "cli-a", "jwt.token.cli"])
    assert scheduled, "CLI 加号必须跑按账号安装序"
    assert scheduled[0] == fresh_app.list_accounts("zai")[0].id


# ── 并发设置 ─────────────────────────────────────────────────────────────────
class TestConcurrencySetting:
    def test_default_2(self, fresh_app):
        assert fresh_app.account_concurrency() == 2

    def test_bad_value_falls_back(self, fresh_app):
        fresh_app.set_setting("account_concurrency", "abc")
        assert fresh_app.account_concurrency() == 2

    def test_negative_clamped(self, fresh_app):
        fresh_app.set_setting("account_concurrency", "-3")
        assert fresh_app.account_concurrency() == 0

    async def test_put_settings_validates(self, fresh_app):
        async with _admin_client(fresh_app) as client:
            res = await client.put("/admin/api/settings",
                                   json={"account_concurrency": 4},
                                   headers={"Authorization": "Bearer zcode"})
            assert res.status_code == 200
            from app.store import store as live_store
            assert live_store.account_concurrency() == 4

            res = await client.put("/admin/api/settings",
                                   json={"account_concurrency": "abc"},
                                   headers={"Authorization": "Bearer zcode"})
            assert res.status_code == 400

            res = await client.get("/admin/api/settings",
                                   headers={"Authorization": "Bearer zcode"})
            assert res.status_code == 200
            assert res.json()["account_concurrency"] == 4
