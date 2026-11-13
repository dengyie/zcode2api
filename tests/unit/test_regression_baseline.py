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
    _is_concurrency_hit,
    _is_exhausted,
    _normalize_body,
)


# ── 模型归一化 ────────────────────────────────────────────────────────────────
class TestNormalizeBody:
    def test_lowercase_alias_mapped(self):
        # glm-5.2 是**已退役**型号（上游 3006），别名表直接改写到 GLM-5.3
        body = {"model": "glm-5.2"}
        assert _normalize_body(body)["model"] == "GLM-5.3"

    def test_provider_prefix_stripped(self):
        # 前缀剥离后仍是退役型号 → 再改写到 GLM-5.3（两层改写都覆盖）
        body = {"model": "bigmodel/GLM-5.2"}
        assert _normalize_body(body)["model"] == "GLM-5.3"

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

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_never_exhausted(self, status):
        """5xx 是上游故障：正文里的 "rate limit exceeded" 等文案不得把号踢成 EXHAUSTED。"""
        assert not _is_exhausted(status, "rate limit exceeded")
        assert not _is_exhausted(status, "insufficient balance")

    @pytest.mark.parametrize("text", ["captcha required", "verify token invalid", "Verify Failed"])
    def test_captcha_error(self, text):
        assert _is_captcha_error(text)

    def test_auth_401_not_captcha(self):
        assert not _is_captcha_error("invalid credentials")


class TestCaptchaChallengeDetection:
    def _resp(self, status: int, headers: dict | None = None):
        import httpx

        return httpx.Response(status, headers=headers or {})

    def test_header_challenge(self):
        from app.constants import CAPTCHA_HEADER
        from app.routes.gateway import _detect_captcha_challenge

        resp = self._resp(200, {CAPTCHA_HEADER: "challenge-param"})
        assert _detect_captcha_challenge(resp, "") == "header"

    def test_body_3007_on_http_200(self):
        """200 + {"code":3007}：上游用 200 承载业务错误时必须仍识别为挑战。"""
        from app.routes.gateway import _detect_captcha_challenge

        resp = self._resp(200)
        assert _detect_captcha_challenge(resp, '{"code":3007,"msg":"verify failed"}') == "in-body-3007"

    def test_text_challenge_on_403(self):
        from app.routes.gateway import _detect_captcha_challenge

        assert _detect_captcha_challenge(self._resp(403), "captcha required") == "text"

    def test_plain_403_not_challenge(self):
        from app.routes.gateway import _detect_captcha_challenge

        assert _detect_captcha_challenge(self._resp(403), "forbidden") is None


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

    def test_invalid_jwt_not_selectable_without_apikey(self):
        acc = self._acc()
        acc.status = Status.INVALID
        assert not acc.is_selectable()
        assert not acc.allows_billing()
        assert acc.uses_plan_channel() is False

    def test_invalid_jwt_selectable_via_apikey_fallback(self):
        acc = self._acc()
        acc.api_key = "sk-fallback"
        acc.status = Status.INVALID
        assert acc.is_selectable()
        assert not acc.allows_billing()
        assert acc.uses_plan_channel() is False

    def test_risk_cooling_jwt_not_selectable_but_claimable(self):
        """风控冷却期整号不可选（即使有 Key 回退；同请求回退由 force_fallback 保证），
        但 billing/claim 照常（领取与使用两条链，2026-11-04 口径）。"""
        acc = self._acc()
        acc.api_key = "sk-fallback"
        acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        assert acc.status == Status.COOLING
        assert acc.enabled is True
        assert not acc.is_selectable()
        # plan 通道名义可用（uses_plan_channel 只管 enabled/invalid/disabled），
        # 冷却靠 is_selectable 挡选号——所以 messages 不会打到它。
        assert acc.uses_plan_channel() is True
        assert acc.allows_billing() is True

    def test_manual_disable_never_selectable_but_claimable(self):
        acc = self._acc()
        acc.api_key = "sk-fallback"
        acc.enabled = False
        acc.status = Status.DISABLED
        assert not acc.is_selectable()
        assert acc.uses_plan_channel() is False
        assert acc.allows_billing() is True

    def test_exhausted_jwt_not_selectable_but_plan_alive(self):
        acc = self._acc()
        acc.status = Status.EXHAUSTED
        assert not acc.is_selectable()
        assert acc.uses_plan_channel() is True
        assert acc.allows_billing() is True

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
        url, headers, _payload = build_request(acc, {}, None)
        assert url == "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages"
        assert headers["Authorization"] == "Bearer a.b.c"
        assert headers["anthropic-version"] == "2023-06-01"
        # JWT 通道带全量身份头 + 追踪头；桌面端账号走 electron/stable
        assert headers["X-Title"] == "Z Code@electron"
        assert headers["X-Release-Channel"] == "production"
        assert "X-Device-Mid" in headers
        assert "x-request-id" in headers and "x-zcode-trace-id" in headers

    def test_apikey_routes_to_fallback(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "plain-key")
        url, headers, _payload = build_request(acc, {}, None)
        assert url == "https://api.z.ai/api/anthropic/v1/messages"
        assert headers["x-api-key"] == "plain-key"

    def test_invalid_jwt_with_apikey_routes_to_fallback(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        acc.api_key = "sk-fallback"
        acc.status = Status.INVALID
        url, headers, _payload = build_request(acc, {}, None)
        assert url == "https://api.z.ai/api/anthropic/v1/messages"
        assert headers["x-api-key"] == "sk-fallback"
        assert "Authorization" not in headers

    def test_captcha_header_injected(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        _, headers, _payload = build_request(acc, {}, "vp-123")
        assert headers["X-Aliyun-Captcha-Verify-Param"] == "vp-123"

    def test_captcha_region_header_injected(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        _, headers, _payload = build_request(acc, {}, "vp-123", None, "cn")
        assert headers["X-Aliyun-Captcha-Verify-Region"] == "cn"

    def test_client_auth_headers_dropped(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        _, headers, _payload = build_request(acc, {}, None, {
            "authorization": "Bearer client", "x-api-key": "leak", "user-agent": "UA/1",
            "x-zcode-foo": "strip", "x-custom": "keep",
        })
        # 客户端的鉴权/身份头被丢弃，authorization 保留的是账号自身凭证
        assert headers["Authorization"] == "Bearer a.b.c"
        # Start Plan 请求不再下发 x-api-key（对齐 Magpie startHeaders：仅
        # Authorization: Bearer JWT），客户端透传的 "leak" 同理被丢弃
        assert "x-api-key" not in headers
        assert headers.get("x-zcode-foo") is None
        assert headers["User-Agent"] != "UA/1"
        assert headers.get("x-custom") == "keep"

    def test_missing_credential_raises(self):
        from app.agent import build_request
        acc = Account.create("zai", "t", "a.b.c")
        acc.jwt_token = None
        with pytest.raises(RuntimeError):
            build_request(acc, {}, None)


# ── billing 阻断文案（只拦「JWT 不可用」；领取 ≠ 使用）────────────────────────
class TestBillingBlockReason:
    def test_cooling_not_blocked(self):
        from app.claim import billing_block_reason
        acc = Account.create("zai", "t", "a.b.c")
        acc.status = Status.COOLING
        acc.cooling_until = time.time() + 100
        assert billing_block_reason(acc) is None

    def test_invalid_jwt(self):
        from app.claim import AUTH_EXPIRED_MESSAGE, billing_block_reason
        acc = Account.create("zai", "t", "a.b.c")
        acc.status = Status.INVALID
        assert billing_block_reason(acc) == AUTH_EXPIRED_MESSAGE

    def test_manual_disable_not_blocked(self):
        from app.claim import billing_block_reason
        acc = Account.create("zai", "t", "a.b.c")
        acc.enabled = False
        acc.status = Status.DISABLED
        assert billing_block_reason(acc, action="上游刷新") is None

    def test_risk_disabled_not_blocked(self):
        from app.claim import billing_block_reason
        acc = Account.create("zai", "t", "a.b.c")
        for _ in range(4):
            acc.risk_penalty(base=900.0, cap=86400.0, ban_strikes=4, decay_seconds=7 * 86400)
        assert acc.status == Status.DISABLED
        # 风控封禁只掐对话通道；billing/claim 照跑（2026-11-04 两条链）
        assert billing_block_reason(acc) is None
        assert acc.uses_plan_channel() is False

    def test_no_jwt_blocked(self):
        from app.claim import billing_block_reason
        acc = Account.create("zai", "t", "not-a-jwt")  # 纯 apiKey
        assert "非 Coding Plan" in (billing_block_reason(acc) or "")
        acc2 = Account.create("zai", "t", "a.b.c")
        acc2.jwt_token = None
        assert "非 Coding Plan" in (billing_block_reason(acc2) or "")


# ── 网关主流程（HTTP 层，走 Mock 上游；夹具见 conftest.py）────────────────────
@pytest.mark.integration
class TestGatewayHTTP:
    async def test_models_endpoint(self, gateway_client):
        client, _ = gateway_client
        res = await client.get("/v1/models")
        assert res.status_code == 200
        ids = [m["id"] for m in res.json()["data"]]
        # Magpie 命名空间前缀：`zai/` 与 `bigmodel/` 前缀供外部 Agent 选 provider
        assert ids == [
            "zai/GLM-5.3-Flash", "zai/GLM-5.3",
            "bigmodel/GLM-5.3-Flash", "bigmodel/GLM-5.3",
        ]

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


# ── 3009 并发识别（2026-11-03 review：禁止裸子串，logid 会撞车）───────────────
class TestConcurrencyHit:
    def test_business_code_3009(self):
        body = '{"code":3009,"msg":"model concurrency limit exceeded","logid":"202611030003009"}'
        assert _is_concurrency_hit(body) is True

    def test_logid_substring_alone_is_not_enough(self):
        """20 位随机 logid 含 "3009" 子串（真实 429 形态）不得误判为并发。"""
        body = '{"code":1302,"msg":"rate limited","logid":"202611030003009xxxx"}'
        assert _is_concurrency_hit(body) is False

    def test_text_fallback_for_non_json(self):
        assert _is_concurrency_hit("model concurrency limit exceeded") is True

    def test_other_codes_are_not_concurrency(self):
        assert _is_concurrency_hit('{"code":1113,"msg":"insufficient balance"}') is False
        assert _is_concurrency_hit("server busy") is False
