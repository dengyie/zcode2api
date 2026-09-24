"""GW-013 请求监控（reqlog + /admin/api/monitoring）集成测试。"""

from __future__ import annotations

import asyncio

import pytest

_GOOD_JWT = "hM.eyJzdWIiOiJtIn0.sig"
_STREAM_JWT = "hS.eyJzdWIiOiJzIn0.sig"      # 流式测试独立 JWT（mock 场景序列按凭证前缀绑定）
_OAI_STREAM_JWT = "hT.eyJzdWIiOiJ0In0.sig"
_BG_JWT = "hB.eyJzdWIiOiJiIn0.sig"          # 后台任务测试独立 JWT（mock 序列按凭证共享，防串场）

ADMIN_AUTH = {"Authorization": "Bearer zcode"}  # 默认后台密钥


@pytest.fixture(autouse=True)
def _clean_reqlog():
    from app import reqlog

    reqlog.clear()
    yield
    reqlog.clear()


@pytest.mark.integration
class TestMessagesBodyValidation:
    async def test_non_object_body_returns_400(self, gateway_client, fresh_app):
        client, _ = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="body-arr")
        res = await client.post("/v1/messages", json=["not", "an", "object"])
        assert res.status_code == 400
        data = res.json()
        assert data["error"]["type"] in ("invalid_request", "invalid_request_error")


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

    async def test_unexpected_dispatch_error_closes_entry(self, gateway_client, fresh_app, monkeypatch):
        """调度层抛非取消异常也必须收口监控条目（2026-09 review P3：
        此前只有 CancelledError 路径调 finish_error，条目会永久滞留「进行中」）。"""
        client, _ = gateway_client
        from app import reqlog
        from app.routes import gateway as gateway_module
        from tests.conftest import seed_account

        seed_account(fresh_app, _GOOD_JWT, name="mon-err")
        async def _boom(*args, **kwargs):
            raise RuntimeError("调度意外爆炸")
        monkeypatch.setattr(gateway_module, "_dispatch", _boom)

        res = await client.post("/v1/messages", json={
            "model": "GLM-5.3", "messages": [{"role": "user", "content": "hi"}],
        })
        assert res.status_code == 500
        entries = reqlog.snapshot()
        assert len(entries) == 1
        e = entries[0]
        assert e["ok"] is False and e["status"] == 500
        assert "调度意外爆炸" in e["error"]

    async def test_safe_refresh_task_holds_strong_ref(self, gateway_client, fresh_app, monkeypatch):
        """成功请求触发的 _safe_refresh 必须被强引用持有（事件循环只持弱引用，
        裸 create_task 会被 GC 静默丢弃 —— 同 main.py 启动安装序已修过的缺陷）。"""
        client, _ = gateway_client
        from app.routes import gateway as gateway_module
        from tests.conftest import seed_account

        seed_account(fresh_app, _BG_JWT, name="mon-bg")
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_refresh(account):
            started.set()
            await release.wait()

        monkeypatch.setattr(gateway_module, "_safe_refresh", _slow_refresh)
        res = await client.post("/v1/messages", json={
            "model": "GLM-5.3-Flash", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert res.status_code == 200
        assert started.is_set()
        # 任务仍在飞行中：必须被模块级强引用持有
        assert len(gateway_module._bg_tasks) >= 1
        release.set()
        await asyncio.sleep(0)

    async def test_messages_stream_success_recorded(self, gateway_client, fresh_app):
        """流式透传路径流结束后也要闭环（finish_ok），tokens 未知记 None。"""
        client, mock = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _STREAM_JWT, name="mon-5")
        res = await client.post("/v1/messages", json={
            "model": "GLM-5.3-Flash", "stream": True, "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert res.status_code == 200
        assert "message_stop" in res.text  # 流已完整消费

        e = (await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)).json()["entries"][0]
        assert e["ok"] is True and e["stream"] is True
        assert e["t_total"] is not None and e["t_first"] is not None
        assert e["input_tokens"] is None  # 透传不解析 SSE，未知 ≠ 0

    async def test_openai_stream_tokens_recorded(self, gateway_client, fresh_app):
        """OpenAI 流式转换器带 usage（message_delta.output_tokens）→ 记录 completion tokens。"""
        client, _ = gateway_client
        from tests.conftest import seed_account

        seed_account(fresh_app, _OAI_STREAM_JWT, name="mon-6")
        res = await client.post("/v1/chat/completions", json={
            "model": "glm-5.3-flash", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert res.status_code == 200
        assert res.text.rstrip().endswith("data: [DONE]")

        e = (await client.get("/admin/api/monitoring", headers=ADMIN_AUTH)).json()["entries"][0]
        assert e["ok"] is True and e["endpoint"] == "chat"
        assert e["output_tokens"] == 5  # mock message_delta 固定 output_tokens=5
        assert e["input_tokens"] is None  # mock 无 message_start usage → 未知

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
