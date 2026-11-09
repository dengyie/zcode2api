"""_messages_egress 出口切换逻辑单元测试。

ZCODE_MESSAGES_FORCE_DIRECT：2026-11-04 线上实证 env 代理出口（GCP）被上游
持续标记 3012（17h+ 零成功），直连是唯一可用出口；开关必须同时驱动
httpx client（trust_env）与 captcha 签发出口（egress tag）。
"""

from __future__ import annotations

from app import egress
from app.routes import gateway

_PROXY_ENV = "http://127.0.0.1:19999"


def test_messages_egress_default_follows_env(monkeypatch):
    monkeypatch.setattr(gateway.settings, "MESSAGES_FORCE_DIRECT", False)
    monkeypatch.setenv("HTTPS_PROXY", _PROXY_ENV)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert gateway._messages_egress() == egress.PROXY


def test_messages_egress_force_direct(monkeypatch):
    monkeypatch.setattr(gateway.settings, "MESSAGES_FORCE_DIRECT", True)
    monkeypatch.setenv("HTTPS_PROXY", _PROXY_ENV)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    # 强制直连优先于 env 代理（captcha tag 与 httpx trust_env 同源切换）
    assert gateway._messages_egress() == egress.DIRECT


def test_messages_egress_no_proxy_available_is_direct(monkeypatch):
    monkeypatch.setattr(gateway.settings, "MESSAGES_FORCE_DIRECT", False)
    # 打桩而非删 env：macOS 上 getproxies() 还会读系统代理（见 app/egress.py
    # 模块注释），删 env 不等于「无代理可走」
    monkeypatch.setattr(gateway.egress, "proxy_url", lambda url=None: None)
    assert gateway._messages_egress() == egress.DIRECT
