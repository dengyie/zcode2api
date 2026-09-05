"""身份头伪装与请求头透传过滤（2026-09-05 review 修复）。

- 平台指纹默认固定 darwin-arm64（constants.CLIENT_PLATFORM 接线）：服务端部署在
  Linux 时 platform.* 会暴露云内核特征，与官方 ZCode 桌面端形状不符。
- x-stainless-*（客户端 Anthropic SDK 自动附加）剔除透传：值来自真实调用客户端，
  与 ZCode/3.10.2 UA 组成矛盾信号；zapi 无 stainless 头长期被上游正常接受。
"""

from __future__ import annotations

import base64
import json

from app import constants
from app.agent import build_request
from app.identity import build_identity_headers
from app.models import Account


def _fake_jwt() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "u-1"}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.sig"


def test_identity_headers_pinned_to_darwin():
    h = build_identity_headers()
    assert h["X-Platform"] == "darwin-arm64"
    assert h["X-Os-Category"] == "macos"
    assert h["X-Os-Version"] == constants.IDENTITY_OS_VERSION
    assert h["User-Agent"] == "ZCode/3.10.2"
    assert h["X-Device-Mid"]


def test_identity_env_override_still_works(monkeypatch):
    monkeypatch.setenv("ZCODE_IDENTITY_PLATFORM", "windows")
    monkeypatch.setenv("ZCODE_IDENTITY_ARCH", "x64")
    monkeypatch.setenv("ZCODE_IDENTITY_RELEASE", "10.0.19045")
    h = build_identity_headers()
    assert h["X-Platform"] == "windows-x64"
    assert h["X-Os-Category"] == "windows"
    assert h["X-Os-Version"] == "10.0.19045"


class TestPassthroughFilter:
    def _build(self, incoming: dict):
        acc = Account(id="x", name="x", provider="zai", mode="jwt", jwt_token=_fake_jwt())
        body = {"model": "GLM-5.2", "messages": [{"role": "user", "content": "hi"}]}
        return build_request(acc, body, None, incoming)[1]

    def test_stainless_and_zcode_dropped(self):
        headers = self._build({
            "X-Stainless-Lang": "js",
            "x-stainless-runtime": "node",
            "X-Zcode-Custom": "nope",
        })
        # 客户端注入的 stainless/自定义 zcode 头被剔除；本服务自生成 trace 头保留
        assert "X-Stainless-Lang" not in headers
        assert "x-stainless-runtime" not in headers
        assert "X-Zcode-Custom" not in headers
        assert "x-zcode-trace-id" in headers

    def test_benign_headers_pass_through(self):
        headers = self._build({"X-Custom-Trace": "keep-me"})
        assert headers["X-Custom-Trace"] == "keep-me"

    def test_identity_headers_not_overridable(self):
        headers = self._build({"X-Device-Mid": "spoofed", "User-Agent": "evil"})
        assert headers["X-Device-Mid"] != "spoofed"
        assert headers["User-Agent"] == "ZCode/3.10.2"
