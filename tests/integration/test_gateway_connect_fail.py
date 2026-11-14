"""GW-009（docs/testing/04 §2）：连接级失败重试 —— 首次断连、降级下一账号。

connect_fail_first 由 Mock 的 _wrap_asgi 在 FastAPI 之外短路（uvicorn
transport.close()，连接上无任何响应字节），客户端 httpx 抛 ReadError ——
网关的 httpx.HTTPError 分支（gateway.py）应把该账号置 cooling 并切下一个。
"""

from __future__ import annotations

import time

import pytest

from app import settings

# JWT 形态同 tests/unit/test_regression_baseline.py（两段 .，3 segments）。
# 注意 h1 前缀已被 baseline 的 test_messages_ok 用作 mock bind（session 级计数器
# 跨用例累加），此处必须用独立前缀 —— connect_fail_first 依赖 n==0 判定。
_FAILING_JWT = "f1.eyJzdWIiOiJhIn0.sig"
_GOOD_JWT = "f2.eyJzdWIiOiJiIn0.sig"


@pytest.mark.integration
class TestConnectFailFailover:
    async def test_first_connect_fail_fails_over(self, gateway_client, fresh_app,
                                                 monkeypatch):
        client, mock = gateway_client
        from tests.conftest import seed_account

        # 连接失败 = 出口链路瞬态（非账号故障）：短冷却，避免高峰期
        # 白白少一个号（2026-11-14 per-account 优化，300s → 60s）
        monkeypatch.setattr(settings, "CONNECT_FAIL_COOLDOWN", 60)
        seed_account(fresh_app, _FAILING_JWT, name="a-fail")
        seed_account(fresh_app, _GOOD_JWT, name="b-good")

        # 按 A 的凭证前缀绑定场景：首次请求真断连，其后恢复 ok
        mock.state.sequences[_FAILING_JWT[:16]] = ["connect_fail_first"]

        res = await client.post("/v1/messages", json={"model": "GLM-5.2",
                                                      "messages": [{"role": "user", "content": "hi"}]})
        assert res.status_code == 200

        # A 被标记 cooling（连接失败），B 正常服务
        accounts = {a.name: a for a in fresh_app.list_accounts("zai")}
        assert accounts["a-fail"].status == "cooling"
        assert "连接失败" in (accounts["a-fail"].last_error or "")
        assert accounts["b-good"].status == "active"
        # 冷却时长 = CONNECT_FAIL_COOLDOWN（60s），而非 300s 全额
        remaining = accounts["a-fail"].cooling_until - time.time()
        assert 50 <= remaining <= 61, f"连接失败冷却应为 ~60s，实际剩余 {remaining:.0f}s"
        # 上游只记录到 B 的成功调用（A 的调用因断连不进端点，无 x-mock-call-index）
        ok_calls = [c for c in mock.state.calls if _GOOD_JWT[:16] in str(c[2])]
        assert len(ok_calls) >= 1
