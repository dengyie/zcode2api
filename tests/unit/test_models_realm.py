"""账号区域（realm）单元测试：

- Account.create 的区域默认与纠偏（bigmodel 提供商 → cn；非法值回退 intl）
- realm 随 to_dict/from_dict 持久化往返
- public_view 暴露 realm
- agent.build_request 回退 Key 通道按区域分流（intl → api.z.ai，cn → open.bigmodel.cn）
"""

from __future__ import annotations

from app import settings
from app.agent import build_request
from app.models import Account


def test_create_zai_defaults_intl():
    acc = Account.create("zai", "a1", "h.payload.sig")
    assert acc.realm == "intl"
    assert acc.mode == "jwt"  # 两段点 + zai 提供商 → JWT


def test_create_bigmodel_defaults_cn():
    acc = Account.create("bigmodel", "a2", "keyid.secret")
    assert acc.realm == "cn"
    assert acc.mode == "apiKey"


def test_create_invalid_realm_falls_back():
    assert Account.create("zai", "a3", "k", realm="xx").realm == "intl"
    assert Account.create("bigmodel", "a4", "k", realm="xx").realm == "cn"


def test_create_explicit_cn_keeps_jwt_mode():
    acc = Account.create("zai", "a5", "h.cn.sig", realm="cn")
    assert acc.realm == "cn"
    assert acc.mode == "jwt"


def test_realm_roundtrip_persistence():
    acc = Account.create("zai", "a6", "h.cn.sig", realm="cn")
    restored = Account.from_dict(acc.to_dict())
    assert restored.realm == "cn"
    # 旧数据（无 realm 字段）加载后回退默认 intl
    legacy = {k: v for k, v in acc.to_dict().items() if k != "realm"}
    assert Account.from_dict(legacy).realm == "intl"


def test_public_view_exposes_realm():
    view = Account.create("zai", "a7", "h.cn.sig", realm="cn").public_view()
    assert view["realm"] == "cn"


def _fallback_url(realm: str) -> str:
    acc = Account.create("zai", "n", "keyid.secret", realm=realm)
    url, headers, _ = build_request(acc, {"model": "GLM-5.3-Flash"}, None)
    assert headers.get("x-api-key") == "keyid.secret"
    return url


def test_fallback_channel_intl_routes_to_zai():
    assert _fallback_url("intl") == settings.UPSTREAM["zai_fallback"]


def test_fallback_channel_cn_routes_to_bigmodel():
    assert _fallback_url("cn") == settings.UPSTREAM["bigmodel"]
