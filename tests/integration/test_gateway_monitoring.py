"""GW-013 请求监控（reqlog + /admin/api/monitoring）集成测试。"""

from __future__ import annotations

import pytest

_GOOD_JWT = "hM.eyJzdWIiOiJtIn0.sig"

ADMIN_AUTH = {"Authorization": "Bearer zcode"}  # 默认后台密钥


@pytest.fixture(autouse=True)
def _clean_reqlog():
    from app import reqlog

    reqlog.clear()
    yield
    reqlog.clear()


@pytest.mark.integration
class TestMonitoringRecording:
    async def test_messages_success_recorded(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="mon-1")
        res = await client.post("/v1/messages", json={
            "model": "GLM-5.3-Flash", "max_tokens": 64,
            "messages": [{"role": "user", "content": "你好世界"}],
        })
        assert res.status_code == 200

        res = await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)
        entries = res.json()["entries"]
        assert len(entries) == 1
        e = entries[0]
        assert e["endpoint"] == "messages"
        assert e["model"] == "GLM-5.3-Flash"
        assert e["account"] == "mon-1"
        assert e["ok"] is True and e["status"] == 200
        assert e["t_first"] is not None and e["t_total"] is not None
        assert "你好世界" in e["preview"]

    async def test_chat_completions_tokens_recorded(self, gateway_client, fresh_app):
        client, _ = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="mon-2")
        res = await client.post("/v1/chat/completions", json={
            "model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}],
        })
        assert res.status_code == 200

        res = await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)
        e = res.json()["entries"][0]
        assert e["endpoint"] == "chat"
        assert e["ok"] is True
        assert e["input_tokens"] == 10 and e["output_tokens"] == 5  # mock 固定 usage

    async def test_no_account_error_recorded(self, fresh_app):
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post("/v1/messages", json={
                "model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}],
            })
            assert res.status_code == 503
            res = await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)
        e = res.json()["entries"][0]
        assert e["ok"] is False and e["status"] == 503
        assert "无可用账号" in e["error"]

    async def test_upstream_4xx_recorded(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="mon-3")
        mock.state.sequences[_GOOD_JWT[:16]] = ["not_found"]
        res = await client.post("/v1/messages", json={
            "model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}],
        })
        assert res.status_code == 404
        e = (await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)).json()["entries"][0]
        assert e["ok"] is False and e["status"] == 404

    async def test_clear_endpoint(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="mon-4")
        await client.post("/v1/messages", json={
            "model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}],
        })
        res = await client.post("/admin/api/monitoring/clear", headers=ADMIN_AUTH)
        assert res.status_code == 200
        res = await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)
        assert res.json()["entries"] == []

    async def test_monitoring_page_served(self, fresh_app):
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            res = await client.get("/admin/monitoring")
        assert res.status_code == 200
        assert "请求监控" in res.text
