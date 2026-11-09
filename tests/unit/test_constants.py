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
    assert constants.MARKETING_TOUCH_URL == "https://zcode.z.ai/api/v1/marketing/touch"
    assert constants.ELECTRON_MANIFEST_URL == "https://zcode.z.ai/api/v1/releases/electron/manifest?platform=darwin-aarch64&channel=1"


def test_set_active_version():
    orig = constants.CLIENT_APP_VERSION
    try:
        constants.set_active_version("3.17.0")
        assert constants.CLIENT_APP_VERSION == "3.17.0"
        assert constants.BILLING_APP_VERSION == "3.17.0"
        assert constants.USER_AGENT == "ZCode/3.17.0"
        assert constants.X_ZCODE_APP_VERSION == "3.17.0"
        assert constants.CLIENT_CONFIGS_QUERY == "app_version=3.17.0"
        # v/V 前缀必须剥离（官方 X-ZCode-App-Version 为纯数字形态，"ZCode/v3"
        # 是指纹漂移）；空串/裸 v 不改动现状
        constants.set_active_version("v3.16.9")
        assert constants.CLIENT_APP_VERSION == "3.16.9"
        assert constants.USER_AGENT == "ZCode/3.16.9"
        constants.set_active_version("V3.16.8")
        assert constants.CLIENT_APP_VERSION == "3.16.8"
        cur = constants.CLIENT_APP_VERSION
        constants.set_active_version("")
        constants.set_active_version("v")
        assert constants.CLIENT_APP_VERSION == cur
    finally:
        constants.set_active_version(orig)


def test_set_active_version_clamped_to_prompt_alignment():
    orig = constants.CLIENT_APP_VERSION
    try:
        # 正文锁：活跃版本永不越过 prompt.mjs 文本所属版本，否则「版本头 +
        # system 正文」错版 → 上游 405/3012。钳制并返回实际生效版本。
        assert constants.set_active_version("3.18.0") == constants.PROMPT_ALIGNED_VERSION
        assert constants.CLIENT_APP_VERSION == constants.PROMPT_ALIGNED_VERSION
        assert constants.USER_AGENT == f"ZCode/{constants.PROMPT_ALIGNED_VERSION}"
        assert constants.set_active_version("9.9.9") == constants.PROMPT_ALIGNED_VERSION
    finally:
        constants.set_active_version(orig)


def test_prompt_aligned_version_is_single_source():
    assert constants.PROMPT_ALIGNED_VERSION == "3.17.2"
    assert constants.DEFAULT_CLIENT_APP_VERSION == constants.PROMPT_ALIGNED_VERSION



def test_client_configs():
    assert constants.CLIENT_CONFIGS_URL == "https://zcode.z.ai/api/v1/client/configs"
    # 实测带 platform 参数上游 3001，只允许 app_version
    assert constants.CLIENT_CONFIGS_QUERY == "app_version=3.17.2"


def test_client_version_single_source():
    # 官方客户端 3.17.2（Magpie packages/zcode APP_VERSION 同源；前值 3.17.1 / 3.16.3）
    # 全部版本出口必须引用同一常量，禁止再出现字面量版本号
    assert constants.CLIENT_APP_VERSION == "3.17.2"
    # 版本头初始值必须等于提示词正文所属版本（否则一启动就错版）
    assert constants.CLIENT_APP_VERSION == constants.PROMPT_ALIGNED_VERSION
    assert constants.X_ZCODE_APP_VERSION == constants.CLIENT_APP_VERSION
    assert constants.USER_AGENT == f"ZCode/{constants.CLIENT_APP_VERSION}"
    assert constants.CLIENT_PLATFORM == "darwin-arm64"  # asar TH() = platform-arch


def test_billing_version_and_activation():
    # billing 族与 messages 指纹版本同源（官方现行版 3.17.2）；zcode-switch 的
    # 「读注册表真实版」在 Linux hub 上收敛为钉常量，升级时改此处即可
    assert constants.BILLING_APP_VERSION == "3.17.2"
    assert constants.BILLING_TITLE == "Z Code@electron"
    assert constants.BILLING_RELEASE_CHANNEL == "production"
    assert constants.EVENT_REPORT_URL == "https://zcode.z.ai/api/v1/event/report"
    assert constants.ACTIVATION_ELEMENTS == ("app_launch", "app_daily_active")
    assert constants.ACTIVATION_SCREEN_RESOLUTION == "2560x1440"


def test_captcha_defaults_match_zcode2api():
    # 实测线上 captcha region=cn（非 zcode2api 的 sgp 兜底），以线上为准
    assert constants.CAPTCHA_DEFAULTS == {
        "enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd",
        # 兜底按「模型请求不带码」处理（2026-11-04 线上实锤：真实配置
        # skip_model_request=true；误带码直接 3012 风控，fail-safe=官方形态）
        "skip_model_request": True,
    }


def test_model_map():
    assert constants.MODEL_NAME_MAP["glm-5.3-flash"] == "GLM-5.3-Flash"
    assert constants.AVAILABLE_MODELS == ["GLM-5.3-Flash", "GLM-5.3"]


def test_retired_models_rewritten_to_live_model():
    """退役型号（上游 3006 model not allowed）必须被改写到当前可用型号。

    2026-10-30 实测：套餐只放行 GLM-5.3 系，5.2/Turbo/5.1/4.7 一律 3006。
    别名表与退役表任一处漏改，客户端就会拿到必然失败的 3006。
    """
    retired = {"GLM-5.2", "GLM-5-Turbo", "GLM-5.1", "GLM-4.7"}
    assert set(constants.RETIRED_MODEL_ALIASES) == retired
    assert set(constants.RETIRED_MODEL_ALIASES.values()) <= set(constants.AVAILABLE_MODELS)
    # 小写别名同样不得映射到退役名（否则别名改写会把请求送回 3006）
    assert set(constants.MODEL_NAME_MAP.values()) <= set(constants.AVAILABLE_MODELS)
    for lower in ("glm-5.2", "glm-5-turbo", "glm-turbo", "glm-5.1", "glm-4.7"):
        mapped = constants.MODEL_NAME_MAP[lower]
        assert mapped == "GLM-5.3", lower


def test_rejection_signals():
    assert constants.EXHAUST_HTTP_STATUSES == (402,)
    assert 1005 in constants.EXHAUST_BUSINESS_CODES  # Plan 通道 exceed quota limit
    assert 1113 in constants.EXHAUST_BUSINESS_CODES  # 无余额/无资源包
    assert 1310 in constants.EXHAUST_BUSINESS_CODES  # 周/月使用上限（Magpie 实测）
    for word in ("余额不足", "额度不足", "exceed quota", "insufficient balance", "使用上限"):
        assert any(word in k for k in constants.EXHAUST_KEYWORDS), word
    # 定量短语而非裸词：裸 "quota" 会把风控/验证码文案误判成额度用完
    assert "quota" not in constants.EXHAUST_KEYWORDS
    assert all(len(k) >= 3 for k in constants.EXHAUST_KEYWORDS)
    # 403 不再无条件判 invalid：需先排除验证码挑战（对齐 zapi classifyAccountFailure）
    assert '"code":3007' in constants.CAPTCHA_BODY_MARKERS


def test_identity_headers():
    # 版本值由 test_client_version_single_source 守护，这里只断言字面量口径
    assert constants.ANTHROPIC_VERSION == "2023-06-01"
    assert constants.X_ZCODE_AGENT == "glm"
    assert constants.HTTP_REFERER == "https://zcode.z.ai"
    assert constants.IDENTITY_TITLE == "Z Code@electron"
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
    # 跨 provider 降级顺序默认 zai → bigmodel（Magpie 模式）
    assert settings.PROVIDER_FALLBACK_ORDER == ("zai", "bigmodel")


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
    assert constants.RISK_BUSINESS_CODE == 3012
    assert "unusual activity" in constants.RISK_CONTROL_MARKERS


def test_retry_settings_defaults():
    from app import settings
    assert settings.RETRY_429_TIMES == 5
    assert settings.RETRY_429_WAIT == 60
    assert settings.RETRY_429_WAIT_MAX == 120
    assert settings.RETRY_5XX_TIMES == 3
    assert settings.RETRY_5XX_WAIT == 5
    assert settings.COOLING_SECONDS == 300
