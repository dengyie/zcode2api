"""constants 收口守护：上游常量的漂移断言（docs/development/03/05 的实证值）。

改动这里的期望值 = 有意变更上游协议口径，必须同步更新 docs/development/05。
"""

from __future__ import annotations

from app import constants


def test_messages_urls():
    assert constants.MESSAGES_URLS["zai"] == \
        "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages"
    assert constants.MESSAGES_URLS["zai_fallback"] == \
        "https://api.z.ai/api/anthropic/v1/messages"
    assert constants.MESSAGES_URLS["bigmodel"] == \
        "https://open.bigmodel.cn/api/anthropic/v1/messages"


def test_billing_base():
    assert constants.BILLING_BASE == "https://zcode.z.ai/api/v1/zcode-plan"


def test_client_configs():
    assert constants.CLIENT_CONFIGS_URL == "https://zcode.z.ai/api/v1/client/configs"
    # 实测带 platform 参数上游 3001，只允许 app_version
    assert constants.CLIENT_CONFIGS_QUERY == "app_version=3.10.2"


def test_client_version_single_source():
    # asar 客户端 gr="3.10.2"；3.0.x 已被上游拒绝（configs 400 / claim 3007）
    # 全部版本出口必须引用同一常量，禁止再出现字面量版本号
    assert constants.CLIENT_APP_VERSION == "3.10.2"
    assert constants.X_ZCODE_APP_VERSION == constants.CLIENT_APP_VERSION
    assert constants.USER_AGENT == f"ZCode/{constants.CLIENT_APP_VERSION}"
    assert constants.X_PLATFORM == "darwin-arm64"  # asar TH() = platform-arch


def test_captcha_defaults_match_zcode2api():
    # 实测线上 captcha region=cn（非 zcode2api 的 sgp 兜底），以线上为准
    assert constants.CAPTCHA_DEFAULTS == {
        "enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd",
    }


def test_model_map():
    assert constants.MODEL_NAME_MAP["glm-5.2"] == "GLM-5.2"
    assert constants.MODEL_NAME_MAP["glm-turbo"] == "GLM-5-Turbo"
    assert constants.AVAILABLE_MODELS == ["GLM-5.2", "GLM-5-Turbo"]


def test_rejection_signals():
    assert constants.EXHAUST_HTTP_STATUSES == (402,)
    assert "余额不足" in constants.EXHAUST_KEYWORDS
    assert "insufficient" in constants.EXHAUST_KEYWORDS
    assert constants.AUTH_INVALID_STATUSES == (401, 403)
    assert constants.RATE_LIMITED_STATUSES == (429,)
    assert constants.CAPTCHA_BODY_CODE == 3007


def test_identity_headers():
    # 版本值由 test_client_version_single_source 守护，这里只断言字面量口径
    assert constants.ANTHROPIC_VERSION == "2023-06-01"
    assert constants.X_ZCODE_AGENT == "glm"
    assert constants.HTTP_REFERER == "https://zcode.z.ai/"


def test_settings_upstream_defaults_from_constants():
    from app import settings
    assert settings.UPSTREAM["zai"] == constants.MESSAGES_URLS["zai"]
    assert settings.UPSTREAM["zai_fallback"] == constants.MESSAGES_URLS["zai_fallback"]
    assert settings.UPSTREAM["bigmodel"] == constants.MESSAGES_URLS["bigmodel"]
    assert settings.ZCODE_BILLING_BASE == constants.BILLING_BASE


# 网关常量（底座原值，回归锁定）
MAX_CAPTCHA_RETRIES = 3
MAX_ACCOUNT_ATTEMPTS = 5


def test_gateway_constants():
    from app.routes import gateway
    assert gateway.MAX_CAPTCHA_RETRIES == MAX_CAPTCHA_RETRIES
    assert gateway.MAX_ACCOUNT_ATTEMPTS == MAX_ACCOUNT_ATTEMPTS
