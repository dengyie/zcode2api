"""CLI 登录入池：OAuth access_token 必须落到 account.oauth_token。

背景：Web 入池（admin_api._schedule_login_followup）登录成功即落 oauth_token，
CLI 入池此前只兑换 API Key、不落该字段，后台 has_oauth_token 标记因此与 Web
路径不一致。该字段仅作信息留存（不参与路由），本用例锁定 CLI 侧不再漏写。
"""

from __future__ import annotations

import cli


class _StubFlow:
    """替身 OAuth 流程：一次性 ready，不发任何网络请求。"""

    def __init__(self) -> None:
        self.exchanged: list[str] = []
        self.polls = 0

    async def init(self) -> tuple[str, str]:
        return "flow-id", "https://example.test/authorize"

    async def poll(self, flow_id: str) -> dict:
        self.polls += 1
        return {
            "status": "ready",
            "zai": {"access_token": "oauth-access-token"},
            "token": "header.payload.signature",
        }

    async def exchange_api_key(self, access_token: str) -> str:
        self.exchanged.append(access_token)
        return "api-key-0001"


async def test_login_persists_oauth_token_for_jwt_account(monkeypatch, fresh_app):
    """JWT 账号入池即带 oauth_token；API Key 账号照旧兑换。"""
    flow = _StubFlow()
    monkeypatch.setattr(cli, "ZaiAuthFlow", lambda: flow)
    monkeypatch.setattr(cli, "store", fresh_app)

    async def _no_followup(acc) -> None:
        return None

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cli, "_cli_ingest_followup", _no_followup)
    # cmd_login 轮询间有 2s sleep，测试里换成空实现（不动全局 asyncio.sleep）
    monkeypatch.setattr(cli, "asyncio", type("_NoSleep", (), {"sleep": staticmethod(_no_sleep)}))

    await cli.cmd_login(["zai", "--no-browser"])

    jwt_acc = next(a for a in fresh_app.list_accounts("zai") if a.jwt_token == "header.payload.signature")
    assert jwt_acc.oauth_token == "oauth-access-token"
    # 后台展示标记（has_oauth_token 只在视图里，不暴露 token 本体）
    assert jwt_acc.public_view()["has_oauth_token"] is True
    # 兑换链路不受影响：API Key 账号仍然入池
    assert any(a.api_key == "api-key-0001" for a in fresh_app.list_accounts("zai"))
