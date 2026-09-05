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


def test_billing_version_and_activation():
    # zcode-switch v1.5.4 实证（2026-09-06 移植）：billing 族用官方现行版 3.11.2，
    # 与 messages 指纹 CLIENT_APP_VERSION（已真机验证）刻意分离
    assert constants.BILLING_APP_VERSION == "3.11.2"
    assert constants.BILLING_TITLE == "Z Code@electron"
    assert constants.BILLING_RELEASE_CHANNEL == "stable"
    assert constants.EVENT_REPORT_URL == "https://zcode.z.ai/api/v1/event/report"
    assert constants.ACTIVATION_ELEMENTS == ("app_launch", "app_daily_active")
    assert constants.ACTIVATION_SCREEN_RESOLUTION == "2560x1440"


def test_captcha_defaults_match_zcode2api():
    # 实测线上 captcha region=cn（非 zcode2api 的 sgp 兜底），以线上为准
    assert constants.CAPTCHA_DEFAULTS == {
        "enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd",
    }


def test_model_map():
    assert constants.MODEL_NAME_MAP["glm-5.3-flash"] == "GLM-5.3-Flash"
    assert constants.MODEL_NAME_MAP["glm-5.2"] == "GLM-5.2"
    assert constants.MODEL_NAME_MAP["glm-turbo"] == "GLM-5-Turbo"
    assert constants.AVAILABLE_MODELS == ["GLM-5.3-Flash", "GLM-5.3"]


def test_rejection_signals():
    assert constants.EXHAUST_HTTP_STATUSES == (402,)
    assert "余额不足" in constants.EXHAUST_KEYWORDS
    assert "insufficient" in constants.EXHAUST_KEYWORDS
    # 403 不再无条件判 invalid：需先排除验证码挑战（对齐 zapi classifyAccountFailure）
    assert '"code":3007' in constants.CAPTCHA_BODY_MARKERS


def test_identity_headers():
    # 版本值由 test_client_version_single_source 守护，这里只断言字面量口径
    assert constants.ANTHROPIC_VERSION == "2023-06-01"
    assert constants.X_ZCODE_AGENT == "glm"
    assert constants.HTTP_REFERER == "https://zcode.z.ai/"
    assert constants.IDENTITY_TITLE == "Z Code@cli"
    assert constants.IDENTITY_RELEASE_CHANNEL == "production"
    assert constants.IDENTITY_CLIENT_LANGUAGE == "zh-CN"
    assert constants.IDENTITY_CLIENT_TIMEZONE == "Asia/Shanghai"
    assert constants.IDENTITY_OS_CATEGORY == "macos"
    assert constants.CAPTCHA_REGION_HEADER == "X-Aliyun-Captcha-Verify-Region"


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


def test_risk_control_signals():
    # 2026-09-05 3012 事件锁定：HTTP 405 承载 {"code":3012,"msg":"...unusual activity..."}
    assert constants.RISK_CONTROL_HTTP_STATUSES == (405,)
    assert '"code":3012' in constants.RISK_CONTROL_MARKERS
    assert "unusual activity" in constants.RISK_CONTROL_MARKERS


def test_retry_settings_defaults():
    from app import settings
    assert settings.RETRY_429_TIMES == 5
    assert settings.RETRY_429_WAIT == 60
    assert settings.RETRY_429_WAIT_MAX == 120
    assert settings.RETRY_5XX_TIMES == 3
    assert settings.RETRY_5XX_WAIT == 5
    assert settings.COOLING_SECONDS == 300
