"""管理鉴权：登录失败节流 + 设置接口不回明文密钥。"""

from __future__ import annotations

import json

import pytest


@pytest.mark.integration
class TestAdminAuthThrottle:
    async def test_failed_logins_lock_out(self, gateway_client, monkeypatch):
        from app import auth_admin

        monkeypatch.setattr(auth_admin, "ADMIN_FAIL_LIMIT", 3)
        auth_admin.reset_failures()
        client, _ = gateway_client

        for _ in range(3):
            res = await client.get("/admin/api/verify",
                                   headers={"Authorization": "Bearer wrong-password"})
            assert res.status_code == 401

        locked = await client.get("/admin/api/verify",
                                  headers={"Authorization": "Bearer zcode"})
        assert locked.status_code == 429

    async def test_success_clears_failures(self, gateway_client, monkeypatch):
        from app import auth_admin

        monkeypatch.setattr(auth_admin, "ADMIN_FAIL_LIMIT", 3)
        auth_admin.reset_failures()
        client, _ = gateway_client

        await client.get("/admin/api/verify",
                         headers={"Authorization": "Bearer wrong-password"})
        ok = await client.get("/admin/api/verify",
                              headers={"Authorization": "Bearer zcode"})
        assert ok.status_code == 200

        for _ in range(3):
            res = await client.get("/admin/api/verify",
                                   headers={"Authorization": "Bearer wrong-password"})
            assert res.status_code == 401
        locked = await client.get("/admin/api/verify",
                                  headers={"Authorization": "Bearer zcode"})
        assert locked.status_code == 429


@pytest.mark.integration
class TestSettingsSecretMask:
    async def test_get_settings_masks_secrets(self, gateway_client, fresh_app):
        client, _ = gateway_client
        fresh_app.set_setting("admin_key", "super-secret-admin-key")
        fresh_app.set_setting("gateway_key", "sk-gateway-secret-value")

        res = await client.get("/admin/api/settings",
                               headers={"Authorization": "Bearer super-secret-admin-key"})
        assert res.status_code == 200
        data = res.json()
        dumped = json.dumps(data)
        assert "super-secret-admin-key" not in dumped
        assert "sk-gateway-secret-value" not in dumped
        assert data["admin_key_set"] is True
        assert data["gateway_key_set"] is True
        assert data["admin_key_is_default"] is False
        assert "admin_key_masked" in data
        assert "gateway_key_masked" in data

    async def test_default_admin_key_flag(self, gateway_client, fresh_app):
        client, _ = gateway_client
        res = await client.get("/admin/api/settings",
                               headers={"Authorization": "Bearer zcode"})
        assert res.status_code == 200
        data = res.json()
        assert data["admin_key_is_default"] is True
        assert data.get("admin_key") in (None, "")

    async def test_put_masked_admin_key_does_not_overwrite(self, gateway_client, fresh_app):
        client, _ = gateway_client
        fresh_app.set_setting("admin_key", "keep-this-admin-key")
        res = await client.put(
            "/admin/api/settings",
            # account_concurrency 已写死（现为 3，上游同限）：PUT 携带该键被静默忽略
            json={"admin_key": "keep…key", "account_concurrency": 3},
            headers={"Authorization": "Bearer keep-this-admin-key"},
        )
        assert res.status_code == 200
        assert fresh_app.admin_key() == "keep-this-admin-key"
        assert fresh_app.account_concurrency() == 3


@pytest.mark.integration
class TestAdminAuthHardening:
    """2.7.33：非 ASCII 密钥、查询串鉴权、导出方法/缓存、密钥强度。"""

    async def test_non_ascii_key_rejected_without_500(self):
        """compare_digest(str) 遇非 ASCII 会 TypeError → 500；必须稳定拒绝。

        HTTP 层发不出非 ASCII 头（httpx 用 ascii 编码），所以直接对比较函数
        断言：不抛 TypeError，且不等于配置值。
        """
        from app import auth_admin

        assert auth_admin._key_equal("密钥🚀", "zcode") is False
        assert auth_admin._key_equal("zcode", "zcode") is True

    async def test_query_param_auth_removed(self, gateway_client):
        """?app_key= 已移除：URL 里的密钥会进日志/历史/Referer。"""
        from app import auth_admin

        auth_admin.reset_failures()
        client, _ = gateway_client
        res = await client.get("/admin/api/verify?app_key=zcode")
        assert res.status_code == 401

    async def test_export_requires_post_and_no_store(self, gateway_client):
        client, _ = gateway_client
        headers = {"Authorization": "Bearer zcode"}
        assert (await client.get("/admin/api/export", headers=headers)).status_code == 405
        res = await client.post("/admin/api/export", headers=headers)
        assert res.status_code == 200
        assert res.headers.get("cache-control") == "no-store"

    async def test_short_gateway_key_rejected(self, gateway_client):
        client, _ = gateway_client
        headers = {"Authorization": "Bearer zcode"}
        res = await client.put("/admin/api/settings", json={"gateway_key": "short"},
                               headers=headers)
        assert res.status_code == 400
        assert "12" in res.json()["detail"]
        # 强密钥可写入
        ok = await client.put("/admin/api/settings",
                              json={"gateway_key": "sk-0123456789abcdef"},
                              headers=headers)
        assert ok.status_code == 200


@pytest.mark.integration
class TestCorsSeparation:
    """前后端分离（2.7.34）：CORS 白名单语义——默认同源，设 ZCODE_CORS_ORIGINS 才放行。"""

    async def test_no_cors_headers_by_default(self):
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app

        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
            r = await c.get("/meta", headers={"Origin": "https://fe.example.com"})
            assert "access-control-allow-origin" not in r.headers

    async def test_cors_origin_allowlisted(self, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from app import settings
        from app.main import create_app

        monkeypatch.setattr(settings, "CORS_ORIGINS", "https://fe.example.com")
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
            r = await c.get("/meta", headers={"Origin": "https://fe.example.com"})
            assert r.headers.get("access-control-allow-origin") == "https://fe.example.com"
            # 未列入的源不放行
            r2 = await c.get("/meta", headers={"Origin": "https://evil.example.com"})
            assert "access-control-allow-origin" not in r2.headers
