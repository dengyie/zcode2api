"""agent.build_request 透传剔除——仿真头不得被客户端值覆盖（2026-11-14 review）。

背景：pass-through 循环会覆盖 build_request 已组装的头；content-type /
anthropic-version 由本服务按通道仿真，客户端透传值一旦覆盖即指纹矛盾。
"""

import base64
import json

from app.agent import build_request
from app.models import Account


def _fake_jwt() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "u-1"}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.sig"


def _build(incoming: dict) -> dict:
    acc = Account(id="x", name="x", provider="zai", mode="jwt", jwt_token=_fake_jwt())
    body = {"model": "GLM-5.3-Flash", "messages": [{"role": "user", "content": "hi"}]}
    return build_request(acc, body, None, incoming)[1]


def test_client_cannot_override_content_type_and_version():
    headers = _build({
        "content-type": "text/plain",          # 客户端乱写的类型
        "anthropic-version": "2023-01-01",     # 客户端的旧版本号
    })
    assert headers["content-type"] == "application/json"
    assert headers["anthropic-version"] != "2023-01-01"


def test_dropped_fingerprint_families_still_dropped():
    headers = _build({
        "user-agent": "evil/1.0",
        "x-device-mid": "forged-mid",
        "x-stainless-lang": "python",
        "x-forwarded-for": "1.2.3.4",
    })
    lowered = {k.lower(): k for k in headers}
    # user-agent / x-device-mid 是仿真生成的合法头——客户端值不得覆盖，但仿真值必须在
    assert headers[lowered["user-agent"]] != "evil/1.0"
    assert headers[lowered["x-device-mid"]] != "forged-mid"
    # 纯剔除族：官方客户端永不携带
    assert "x-stainless-lang" not in lowered
    assert "x-forwarded-for" not in lowered


def test_benign_headers_still_pass_through():
    headers = _build({"X-Safe-Header": "keep-me"})
    assert headers["X-Safe-Header"] == "keep-me"
