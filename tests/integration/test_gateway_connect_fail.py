"""GW-009（docs/testing/04 §2）：连接级失败重试 —— 首次断连、降级下一账号。

connect_fail_first 由 Mock 的 _wrap_asgi 在 FastAPI 之外短路（uvicorn
transport.close()，连接上无任何响应字节），客户端 httpx 抛 ReadError ——
网关的 httpx.HTTPError 分支（gateway.py）应把该账号置 cooling 并切下一个。
"""

from __future__ import annotations

import pytest

# 与 tests/unit/test_regression_baseline.py 同款 JWT 形态（两段 .，3 segments）
_FAILING_JWT = "h1.eyJzdWIiOiJhIn0.sig"
_GOOD_JWT = "h2.eyJzdWIiOiJiIn0.sig"


@pytest.mark.integration
class TestConnectFailFailover:
    async def test_first_connect_fail_fails_over(self, gateway_client, fresh_app):
        client, mock = gateway_client
        from tests.conftest import seed_account

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
        # 上游只记录到 B 的成功调用（A 的调用因断连不进端点，无 x-mock-call-index）
        ok_calls = [c for c in mock.state.calls if _GOOD_JWT[:16] in str(c[2])]
        assert len(ok_calls) >= 1
