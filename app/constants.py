"""上游常量收口 —— 所有与 zcode/zai/bigmodel 上游相关的 URL、模型名、判定关键字集中在此。

约定（docs/development/06-dev-guide.md）：
- 模块代码禁止再硬编码上游 URL / 模型名 / 关键字，一律 import 本模块。
- 改动任何常量必须先在 tests/unit/test_constants.py 补断言，防止无意漂移。
- 值均来自源码实证，出处标注见 docs/development/05-upstream-protocols.md。
"""

from __future__ import annotations

# ── 上游 origin ──────────────────────────────────────────────────────────────
# Plan 通道（JWT + 验证码）：zcode.z.ai 的 coding-plan 代理端点
ZCODE_ORIGIN = "https://zcode.z.ai"
# API Key 通道（回退 / bigmodel 同形）：api.z.ai 的 anthropic 兼容端点
ZAI_API_ORIGIN = "https://api.z.ai"
# bigmodel（智谱开放平台）anthropic 兼容端点
BIGMODEL_ORIGIN = "https://open.bigmodel.cn"

# ── messages 端点（settings.UPSTREAM 的默认值来源）────────────────────────────
MESSAGES_PATHS = {
    # zai + jwt（Plan 通道，需 X-Aliyun-Captcha-Verify-Param）
    "zai": "/api/v1/zcode-plan/anthropic/v1/messages",
    # zai + apiKey 回退通道（免验证码）
    "zai_fallback": "/api/anthropic/v1/messages",
    "bigmodel": "/api/anthropic/v1/messages",
}
MESSAGES_URLS = {
    "zai": ZCODE_ORIGIN + MESSAGES_PATHS["zai"],
    "zai_fallback": ZAI_API_ORIGIN + MESSAGES_PATHS["zai_fallback"],
    "bigmodel": BIGMODEL_ORIGIN + MESSAGES_PATHS["bigmodel"],
}

# ── 计费 / 额度端点（quota.py / 未来 claim.py）────────────────────────────────
BILLING_BASE = f"{ZCODE_ORIGIN}/api/v1/zcode-plan"
BILLING_CURRENT_PATH = "/billing/current"
BILLING_BALANCE_PATH = "/billing/balance"
USAGE_PATH = "/usage"
# WAF 风险点：billing/* 连续查询易触发拦截，轮询必须错峰（docs 05 §风险控制）

# ── OAuth ────────────────────────────────────────────────────────────────────
OAUTH_CLI_INIT_PATH = "/api/v1/oauth/cli/init"
OAUTH_CLI_POLL_PATH = "/api/v1/oauth/cli/poll"   # + /{flow_id}

# ── 客户端配置（公开、免鉴权）─────────────────────────────────────────────────
CLIENT_CONFIGS_URL = f"{ZCODE_ORIGIN}/api/v1/client/configs"
# 上游校验 app_version（3.0.0 已 400）；实测带 platform 参数直接 3001，只带版本号
CLIENT_CONFIGS_QUERY = "app_version=3.10.2"
# 免登录额度口径（免费 Start Plan 日额度），仅作展示参考，不参与状态机
FREE_QUOTA = {"GLM-5.3": 3_000_000, "GLM-5-Turbo": 2_000_000}

# ── 验证码默认配置（client/configs 拉取失败时的兜底）──────────────────────────
CAPTCHA_DEFAULTS = {"enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd"}

# ── 模型名 ───────────────────────────────────────────────────────────────────
# Z.AI 上游模型名大小写敏感；客户端传小写别名时映射到官方名（gateway.MODEL_NAME_MAP）
MODEL_NAME_MAP = {
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
}
# /v1/models 对外公布
AVAILABLE_MODELS = ["GLM-5.2", "GLM-5-Turbo"]

# ── 请求头 ───────────────────────────────────────────────────────────────────
ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = "ZCode/3.0.1"
X_ZCODE_APP_VERSION = "3.0.1"
X_ZCODE_AGENT = "glm"
HTTP_REFERER = "https://zcode.z.ai/"
CAPTCHA_HEADER = "X-Aliyun-Captcha-Verify-Param"

# ── 上游被拒信号 → 账号动作（gateway.classify / quota 判定共用）────────────────
EXHAUST_HTTP_STATUSES = (402,)
EXHAUST_KEYWORDS = ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足")
CAPTCHA_KEYWORDS = ("captcha", "verify token", "verify failed")
AUTH_INVALID_STATUSES = (401, 403)
RATE_LIMITED_STATUSES = (429,)
# 验证码挑战：HTTP 403 + 文案，或 HTTP 400 + body {"code":3007}（docs 05 §被拒信号表）
CAPTCHA_BODY_CODE = 3007
