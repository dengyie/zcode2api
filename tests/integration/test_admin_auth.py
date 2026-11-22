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

    async def test_spoofed_proxy_header_cannot_bypass_lockout(self, gateway_client, monkeypatch):
        """P1 信任边界（2026-11-21 review）：TRUST_PROXY_HEADERS 缺省关时，
        伪造 cf-connecting-ip/x-real-ip 不能让每个请求拿到独立的失败计数器
        —— 锁定按 socket 对端地址（测试客户端同一对端）计数。"""
        from app import auth_admin

        monkeypatch.setattr(auth_admin, "ADMIN_FAIL_LIMIT", 3)
        auth_admin.reset_failures()
        client, _ = gateway_client

        for i in range(3):
            res = await client.get(
                "/admin/api/verify",
                headers={"Authorization": "Bearer wrong-password",
                         "cf-connecting-ip": f"10.9.9.{i}",
                         "x-real-ip": f"10.8.8.{i}"})
            assert res.status_code == 401
        # 同一对端已锁：换一个「新伪造 IP」也绕不过
        locked = await client.get(
            "/admin/api/verify",
            headers={"Authorization": "Bearer zcode",
                     "cf-connecting-ip": "10.9.9.99"})
        assert locked.status_code == 429

    async def test_failures_map_capacity_bounded(self, monkeypatch):
        """P1 容量上界（2026-11-21 review）：_failures 逐出最旧条目，
        无界 dict 撑内存的 DoS 面被封顶。"""
        from app import auth_admin

        auth_admin.reset_failures()
        old_max = auth_admin._FAILURES_MAX
        monkeypatch.setattr(auth_admin, "_FAILURES_MAX", 4)
        try:
            for i in range(10):
                auth_admin._record_failure(f"ip-{i}")
            assert len(auth_admin._failures) <= 4
            # 最早插入的已被逐出
            assert "ip-0" not in auth_admin._failures
            assert "ip-9" in auth_admin._failures
        finally:
            auth_admin._FAILURES_MAX = old_max
            auth_admin.reset_failures()

    async def test_gateway_key_failure_also_locks(self, gateway_client, monkeypatch):
        """P3（2026-11-21 review）：/v1 网关 key 暴力试 key 也有失败锁定。"""
        from app import auth_admin

        monkeypatch.setattr(auth_admin, "ADMIN_FAIL_LIMIT", 3)
        auth_admin.reset_failures()
        client, _ = gateway_client
        # fresh_app 已把 store 单例重绑到用例独立实例；gateway key 走 store 设置
        from app.store import store

        store.set_setting("gateway_key", "sk-real-gateway-key-123")

        for _ in range(3):
            res = await client.post("/v1/messages", json={"model": "m", "messages": []},
                                    headers={"Authorization": "Bearer wrong-key"})
            assert res.status_code in (401, 403)
        locked = await client.post("/v1/messages", json={"model": "m", "messages": []},
                                   headers={"Authorization": "Bearer sk-real-gateway-key-123"})
        assert locked.status_code == 429

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
        # GET 必须被拒（405 方法不符；/admin 静态挂载下为 404 文件不存在——语义等价：不放行）
        assert (await client.get("/admin/api/export", headers=headers)).status_code in (404, 405)
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


@pytest.mark.asyncio
async def test_loopback_listener_adopts_proxy_headers(gateway_client, monkeypatch):
    """迭代二 P1 豁免正例（2026-11-22 迭代三收口）：进程只监听回环
    （settings.HOST=127.0.0.1，tebi canonical 部署形态）且 socket 对端为
    127.0.0.1 时——外部不可直连，请求只能来自本机隧道进程（cloudflared，
    必然覆写 cf-connecting-ip）——按代理头锁真实公网来源是安全的：不同伪造头
    = 不同计数器，锁定按真实来源分键。"""
    from app import auth_admin, settings

    monkeypatch.setattr(auth_admin, "ADMIN_FAIL_LIMIT", 3)
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    monkeypatch.delenv("ZCODE_TRUST_PROXY_HEADERS", raising=False)
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    auth_admin.reset_failures()
    client, _ = gateway_client

    # 三个「不同来源」各失败 2 次（不达 3），互不连坐——豁免路径采信代理头
    for real_ip in ("203.0.113.10", "203.0.113.11", "203.0.113.12"):
        for _ in range(2):
            res = await client.get(
                "/admin/api/verify",
                headers={"Authorization": "Bearer wrong",
                         "cf-connecting-ip": real_ip})
            assert res.status_code == 401
    # 第四个「来源」干净可连
    res = await client.get("/admin/api/verify",
                           headers={"Authorization": "Bearer zcode",
                                    "cf-connecting-ip": "203.0.113.99"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_direct_bind_listener_ignores_proxy_headers(gateway_client, monkeypatch):
    """迭代二 P1 豁免负例：监听 0.0.0.0 的直连部署上 socket=127.0.0.1 也可能
    是本机 curl——伪造 cf-connecting-ip 不得绕过失败锁定（对齐 2026-11-21
    P1 修复语义，豁免双条件缺一不可）。"""
    from app import auth_admin, settings

    monkeypatch.setattr(auth_admin, "ADMIN_FAIL_LIMIT", 3)
    monkeypatch.setattr(settings, "HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
    auth_admin.reset_failures()
    client, _ = gateway_client

    for i in range(3):
        res = await client.get(
            "/admin/api/verify",
            headers={"Authorization": "Bearer wrong",
                     "cf-connecting-ip": f"10.9.9.{i}"})
        assert res.status_code == 401
    # 同一对端已锁：换伪造 IP 绕不过
    locked = await client.get("/admin/api/verify",
                              headers={"Authorization": "Bearer zcode",
                                       "cf-connecting-ip": "10.9.9.99"})
    assert locked.status_code == 429
