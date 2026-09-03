"""回归基线：zcode2api 原行为锁定（M0 出口标准）。

这些用例描述的是**底座既有行为**，不是新设计 —— 改动底座时它们必须保持绿色，
若有意变更行为，先改用例并在 PR 里说明（docs/development/06 §测试纪律）。
"""

from __future__ import annotations

import json
import time

import pytest

from app.models import Account, Status
from app.routes.gateway import (
    _detect_provider,
    _is_captcha_error,
    _is_exhausted,
    _normalize_body,
)


# ── 模型归一化 ────────────────────────────────────────────────────────────────
class TestNormalizeBody:
    def test_lowercase_alias_mapped(self):
        body = {"model": "glm-5.2"}
        assert _normalize_body(body)["model"] == "GLM-5.2"

    def test_provider_prefix_stripped(self):
        body = {"model": "bigmodel/GLM-5.2"}
        assert _normalize_body(body)["model"] == "bigmodel/GLM-5.2".split("/")[-1]

    def test_unknown_model_passed_through(self):
        body = {"model": "glm-unknown"}
        assert _normalize_body(body)["model"] == "glm-unknown"

    def test_string_content_bridged_to_blocks(self):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        assert _normalize_body(body)["messages"][0]["content"] == [{"type": "text", "text": "hi"}]

    def test_block_content_untouched(self):
        blocks = [{"type": "text", "text": "hi"}]
        body = {"messages": [{"role": "user", "content": blocks}]}
        assert _normalize_body(body)["messages"][0]["content"] is blocks


# ── provider 判定 ─────────────────────────────────────────────────────────────
class TestDetectProvider:
    def test_default_zai(self):
        assert _detect_provider({}, {}) == "zai"

    def test_model_prefix(self):
        assert _detect_provider({"model": "bigmodel/GLM-5.2"}, {}) == "bigmodel"

    def test_header(self):
        assert _detect_provider({}, {"x-provider": "bigmodel"}) == "bigmodel"


# ── 被拒信号判定（值以 constants 为唯一来源）───────────────────────────────────
class TestRejectionSignals:
    def test_402_is_exhausted(self):
        assert _is_exhausted(402, "")

    @pytest.mark.parametrize("text", ["insufficient balance", "quota exceeded", "余额不足", "额度不足"])
    def test_keyword_exhausted(self, text):
        assert _is_exhausted(400, text)

    def test_plain_400_not_exhausted(self):
        assert not _is_exhausted(400, "bad request")

    @pytest.mark.parametrize("text", ["captcha required", "verify token invalid", "Verify Failed"])
    def test_captcha_error(self, text):
        assert _is_captcha_error(text)

    def test_auth_401_not_captcha(self):
        assert not _is_captcha_error("invalid credentials")


# ── Account 状态机（models.py 原语义）─────────────────────────────────────────
class TestAccountStateMachine:
    def _acc(self, **kw) -> Account:
        return Account.create("zai", "t", "a.b.c", **kw)

    def test_jwt_detection(self):
        acc = self._acc()
        assert acc.mode == "jwt"

    def test_apikey_detection(self):
        acc = Account.create("zai", "t", "not-a-jwt")
        assert acc.mode == "apiKey"

    def test_selectable_default(self):
        assert self._acc().is_selectable()

    def test_exhausted_not_selectable(self):
        acc = self._acc()
        acc.status = Status.EXHAUSTED
        assert not acc.is_selectable()

    def test_cooling_until_expiry(self):
        acc = self._acc()
        acc.status = Status.COOLING
        acc.cooling_until = time.time() - 1
        assert acc.is_selectable()

    def test_cooling_within_window(self):
        acc = self._acc()
        acc.status = Status.COOLING
        acc.cooling_until = time.time() + 100
        assert not acc.is_selectable()

    def test_effective_status_after_cooldown(self):
        acc = self._acc()
        acc.status = Status.COOLING
        acc.cooling_until = time.time() - 1
        assert acc.effective_status() == Status.ACTIVE

    def test_public_view_masks_token(self):
        acc = Account.create("zai", "t", "x" * 100)
        view = acc.public_view()
        assert "x" * 100 not in json.dumps(view)
        assert view["token_masked"].startswith("x")


# ── Store 轮询（store.py 原语义）──────────────────────────────────────────────
class TestStoreRotation:
    def test_round_robin_distribution(self, fresh_app):
        for i in range(3):
            fresh_app.add_account("zai", f"a{i}", f"jwt.token.{i}")
        picks = [fresh_app.select("zai").id for _ in range(9)]
        assert len(set(picks)) == 3
        assert all(picks.count(p) == 3 for p in set(picks))

    def test_skip_ids(self, fresh_app):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        b = fresh_app.add_account("zai", "b", "jwt.token.b")
        pick = fresh_app.select("zai", skip_ids={a.id})
        assert pick.id == b.id

    def test_duplicate_secret_dedup(self, fresh_app):
        fresh_app.add_account("zai", "a", "jwt.token.same")
        again = fresh_app.add_account("zai", "b", "jwt.token.same")
        assert len(fresh_app.list_accounts("zai")) == 1
        assert again.name == "a"

    def test_disabled_skipped(self, fresh_app):
        a = fresh_app.add_account("zai", "a", "jwt.token.a")
        fresh_app.set_enabled("zai", a.id, False)
        assert fresh_app.select("zai") is None

    def test_persistence_roundtrip(self, fresh_app, tmp_path):
        fresh_app.add_account("zai", "a", "jwt.token.a")
        from app.store import Store
        reloaded = Store()
        assert [x.name for x in reloaded.list_accounts("zai")] == ["a"]

    def test_export_import(self, fresh_app):
        fresh_app.add_account("zai", "a", "jwt.token.a")
        data = fresh_app.export()
        fresh_app.remove_account("zai", "a")
        assert fresh_app.import_accounts(data) == 1


# ── 请求构建（agent.py 原语义）────────────────────────────────────────────────
class TestBuildRequest:
    def test_jwt_routes_to_plan_channel(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        url, headers = build_request(acc, {}, None)
        assert url == "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages"
        assert headers["Authorization"] == "Bearer a.b.c"
        assert headers["anthropic-version"] == "2023-06-01"

    def test_apikey_routes_to_fallback(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "plain-key")
        url, headers = build_request(acc, {}, None)
        assert url == "https://api.z.ai/api/anthropic/v1/messages"
        assert headers["x-api-key"] == "plain-key"

    def test_captcha_header_injected(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        _, headers = build_request(acc, {}, "vp-123")
        assert headers["X-Aliyun-Captcha-Verify-Param"] == "vp-123"

    def test_client_auth_headers_dropped(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        _, headers = build_request(acc, {}, None, {
            "authorization": "Bearer client", "x-api-key": "leak", "user-agent": "UA/1",
            "x-zcode-foo": "strip", "x-custom": "keep",
        })
        # 客户端的鉴权/身份头被丢弃，authorization 保留的是账号自身凭证
        assert headers["Authorization"] == "Bearer a.b.c"
        assert "x-api-key" not in {h.lower() for h in headers}
        assert headers.get("x-zcode-foo") is None
        assert headers["User-Agent"] != "UA/1"
        assert headers.get("x-custom") == "keep"

    def test_missing_credential_raises(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        acc.jwt_token = None
        with pytest.raises(RuntimeError):
            build_request(acc, {}, None)


# ── 网关主流程（HTTP 层，走 Mock 上游；夹具见 conftest.py）────────────────────
@pytest.mark.integration
class TestGatewayHTTP:
    async def test_models_endpoint(self, gateway_client):
        client, _ = gateway_client
        res = await client.get("/v1/models")
        assert res.status_code == 200
        ids = [m["id"] for m in res.json()["data"]]
        assert ids == ["GLM-5.2", "GLM-5-Turbo"]

    async def test_messages_ok(self, gateway_client):
        client, upstream = gateway_client
        fresh_account("h1.eyJzdWIiOiJhIn0.sig")
        res = await client.post("/v1/messages", json={
            "model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}],
        })
        assert res.status_code == 200
        assert res.json()["content"][0]["text"] == "Hello from mock upstream"
        # 上游收到归一化后的模型名与鉴权头
        method, path, headers, _ = upstream.state.calls[-1]
        assert path == "/api/v1/zcode-plan/anthropic/v1/messages"

    async def test_no_account_503(self, gateway_client):
        client, _ = gateway_client
        res = await client.post("/v1/messages", json={"model": "glm-5.2", "messages": []})
        assert res.status_code == 503
        assert res.json()["error"]["type"] == "no_available_account"


def fresh_account(secret: str) -> Account:
    """向当前 store 注入账号（gateway_client 用 fresh_app 的 store 单例）。"""
    from app.store import store
    return store.add_account("zai", "t", secret)
