"""网关 API Key 鉴权（auth_admin.verify_gateway_key）。

key 未配置 → 放行（空 key fallback）；已配置 → 无凭证 401 / 错凭证 403 /
`Authorization: Bearer` 与 `x-api-key` 双通道放行。fresh_app 已把全局 store
重绑到隔离实例，用 store.set_setting 落 key、用例收尾清空还原（顺带覆盖
set_setting/get_setting 链）。
"""

from __future__ import annotations

import pytest

from tests.conftest import seed_account

_GATEWAY_KEY = "sk-test-gateway-0001"
_MESSAGES_BODY = {"model": "GLM-5.2",
                  "messages": [{"role": "user", "content": "hi"}]}
# JWT 前缀是 mock 上游按 bind 键控 counters 的依据（session 级共享），
# 不能与 test_gateway_connect_fail 的 _FAILING_JWT 撞前缀，否则其
# "首次请求断连" 的 n==0 判定被这里的调用破坏。
_AUTH_JWT = "h9.eyJzdWIiOiJhdXRoIn0.sig"


@pytest.fixture
def gateway_key(fresh_app):
    """配置网关 key，用例结束清空还原（未配置放行语义）。

    依赖 fresh_app 保证先重绑全局 store —— 写原始 store 是无效的。
    """
    fresh_app.set_setting("gateway_key", _GATEWAY_KEY)
    yield _GATEWAY_KEY
    fresh_app.set_setting("gateway_key", "")


@pytest.mark.integration
class TestGatewayAuth:
    async def test_no_key_configured_allows_anonymous(self, gateway_client, fresh_app):
        """未配置 key 时保持放行（现有部署的默认形态，回归锁定）。"""
        assert fresh_app.gateway_key() == ""
        res = await gateway_client[0].post("/v1/messages", json=_MESSAGES_BODY)
        assert res.status_code != 401 and res.status_code != 403

    async def test_missing_key_rejected_401(self, gateway_client, fresh_app, gateway_key):
        client, _mock = gateway_client
        seed_account(fresh_app, _AUTH_JWT, name="auth-a")
        res = await client.post("/v1/messages", json=_MESSAGES_BODY)
        assert res.status_code == 401

    async def test_wrong_key_rejected_403(self, gateway_client, fresh_app, gateway_key):
        client, _mock = gateway_client
        seed_account(fresh_app, _AUTH_JWT, name="auth-b")
        res = await client.post("/v1/messages", json=_MESSAGES_BODY,
                                headers={"x-api-key": "sk-wrong"})
        assert res.status_code == 403

    async def test_bearer_and_x_api_key_both_accepted(self, gateway_client, fresh_app, gateway_key):
        client, mock = gateway_client
        seed_account(fresh_app, _AUTH_JWT, name="auth-c")
        for headers in ({"Authorization": f"Bearer {gateway_key}"},
                        {"x-api-key": gateway_key}):
            res = await client.post("/v1/messages", json=_MESSAGES_BODY, headers=headers)
            assert res.status_code == 200, headers

    async def test_models_endpoint_gated_too(self, gateway_client, fresh_app, gateway_key):
        """/v1/models 与 /v1/messages 同闸门。"""
        client, _mock = gateway_client
        assert (await client.get("/v1/models")).status_code == 401
        assert (await client.get("/v1/models",
                                 headers={"x-api-key": gateway_key})).status_code == 200
