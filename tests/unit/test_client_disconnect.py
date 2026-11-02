"""ClientDisconnect→400：客户端在读请求体阶段断开时静默回包（2.7.12 引入，2.7.13 补测）。

背景：断开时 starlette 的 `request.json()` 抛 `ClientDisconnect`，未被接住会
让 err.log 每次刷一条完整堆栈（线上实锤）。三个 JSON 解析点（messages /
`_run_compat_endpoint` / count_tokens）统一捕获后回 400——回包无人收，
目的只是不放大失败。

测法：stub Request 直呼路由函数（绕过鉴权依赖——这里测的是解析分支而非
鉴权；stub 的 json() 首行即抛，headers 等后续属性不会被触达）。
"""

from __future__ import annotations

from starlette.requests import ClientDisconnect

from app.routes.gateway import _run_compat_endpoint, count_tokens, messages


class _Disconnected:
    """`request.json()` 即断开的 stub。"""

    headers = {}

    async def json(self):
        raise ClientDisconnect()


async def test_messages_disconnect_returns_400():
    resp = await messages(_Disconnected())  # type: ignore[arg-type]
    assert resp.status_code == 400
    assert b"invalid_request" in resp.body


async def test_count_tokens_disconnect_returns_400():
    resp = await count_tokens(_Disconnected())  # type: ignore[arg-type]
    assert resp.status_code == 400
    assert b"invalid_request" in resp.body


async def test_compat_endpoint_disconnect_returns_400():
    async def _translate(payload):  # 断开先于翻译发生，占位即可
        return {"model": "m", "messages": []}, None

    resp = await _run_compat_endpoint(
        _Disconnected(), "chat", _translate, None, None)  # type: ignore[arg-type]
    assert resp.status_code == 400
    assert b"invalid_request_error" in resp.body
