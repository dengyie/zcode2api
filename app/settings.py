"""运行期配置：环境变量 + 默认值。

所有可调参数集中在此。账号与凭证不在此处，而是持久化到 data/ 目录（见 store.py）。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from . import constants

load_dotenv()

# 项目根目录
ROOT_DIR = Path(__file__).resolve().parents[1]


def _resolve_path(env_name: str, default: str) -> Path:
    raw = (os.getenv(env_name, default) or default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _int(env_name: str, default: int) -> int:
    try:
        return int(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(env_name: str, default: bool) -> bool:
    raw = (os.getenv(env_name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ── 目录 ─────────────────────────────────────────────────────────────────────
DATA_DIR = _resolve_path("ZCODE_DATA_DIR", "data")
# 账号与设置持久化到本地 SQLite（与 grok2api 的 local 后端一致）
DB_PATH = DATA_DIR / "accounts.db"
# 前端目录（前后端分离）：默认仓库根 frontend/，可用 ZCODE_FRONTEND_DIR 指向
# 独立部署目录（线上 /data/zcode-hub/frontend）；包内 statics 仅作兜底
FRONTEND_DIR = _resolve_path(
    "ZCODE_FRONTEND_DIR",
    "frontend" if (ROOT_DIR / "frontend").is_dir() else str(Path(__file__).resolve().parent / "statics"),
)

# ── 服务 ─────────────────────────────────────────────────────────────────────
PORT = _int("ZCODE_PORT", 3000)
HOST = os.getenv("ZCODE_HOST", "0.0.0.0")

# ── 鉴权 ─────────────────────────────────────────────────────────────────────
# 后台管理密码默认值，首次启动写入 data/accounts.db，之后以数据库（meta 表）为准。
DEFAULT_ADMIN_KEY = os.getenv("ZCODE_ADMIN_KEY", "zcode")

# ── 验证码 ───────────────────────────────────────────────────────────────────
# 预解 token 池。真浏览器求解重（单枚 10–40s、Chromium 数百 MB），池收敛为 1/2
#（TTL 95s → 稳态约 95s 解一枚，Chromium 占空比 ~10–40%）；回滚 legacy 可调大。
CAPTCHA_POOL_MIN = _int("CAPTCHA_POOL_MIN", 1)        # 目标库存（低于则补）
CAPTCHA_POOL_MAX = _int("CAPTCHA_POOL_MAX", 2)        # 池上限
CAPTCHA_TOKEN_TTL = _int("CAPTCHA_TOKEN_TTL", 95_000) # 单枚 token 最大可用时长（ms；上游实际 ~2min）
CAPTCHA_CONFIG_CACHE_TTL = _int("CAPTCHA_CONFIG_CACHE_TTL", 600_000)  # ms

# 验证码求解（真浏览器：puppeteer-core + 系统 Chromium 跑阿里云官方无痕 SDK，
# 对齐 zcode-switch captcha.js。happy-dom 路线 2026-09 起被风控「unusual
# activity」全拒，solver.js 仅留作回滚：ZCODE_CAPTCHA_SOLVER=legacy）
NODE_PATH = os.getenv("ZCODE_NODE_PATH", "node")
CAPTCHA_SOLVER_DIR = ROOT_DIR / "captcha_node"
CAPTCHA_SOLVER_MODE = (os.getenv("ZCODE_CAPTCHA_SOLVER", "pw").strip().lower() or "pw")
CAPTCHA_SOLVER_JS = CAPTCHA_SOLVER_DIR / (
    "solver.js" if CAPTCHA_SOLVER_MODE == "legacy" else "solver_pw.js"
)
# Chromium 可执行文件（solver_pw.js 用；env 可覆盖，缺省按常见路径探测）
CHROMIUM_PATH = os.getenv("ZCODE_CHROMIUM_PATH", "/usr/local/bin/chromium")
CAPTCHA_SOLVE_RETRIES = _int("ZCODE_CAPTCHA_RETRIES", 4)
# 每次求解超时（秒）：真浏览器含 launch（内存压力下可 30s+）+ SDK 加载 + 无痕验证，
# 且 solver 进程内自旋重试 3 次（约 40s×3），须容得下
CAPTCHA_SOLVE_TIMEOUT = _int("ZCODE_CAPTCHA_TIMEOUT", 240)
# 同时运行的求解器进程上限（含预热与热路径现解）。每次求解是一个真 Chromium
# （数百 MB + 单核），池空突发时若无上限会按并发请求数同时拉起 N 个浏览器，
# 在 1–2GB 小机上直接把内存打满（2.7.33）。超出的求解排队，而不是并行。
CAPTCHA_MAX_CONCURRENT_SOLVES = max(1, _int("ZCODE_CAPTCHA_MAX_SOLVES", 2))
# 预热哪个出口的 token 池：空 = 跟随 messages 上游的实际出口（推荐）。
# 可强制 "proxy" / "direct"；非预热出口按需现解（claim 一小时一轮，够用）。
CAPTCHA_POOL_EGRESS = (os.getenv("ZCODE_CAPTCHA_POOL_EGRESS", "").strip().lower() or "")

# ── 用量监控 ─────────────────────────────────────────────────────────────────
# 后台自动刷新账号额度的间隔（秒）。0 表示关闭后台轮询，仅按需刷新。
# 默认 1800（2.6.5 整改）：zcode-switch 参照系下额度查询是用户开界面才触发
# （人节奏，日均几十次）；60s 轮询 ≈ 每号每天 4300+ billing 请求，是风控
# 「unusual activity」的主信号源（vault「billing 连续查询易触发拦截」落地）。
QUOTA_REFRESH_INTERVAL = _int("ZCODE_QUOTA_REFRESH_INTERVAL", 1800)
# 成功对话后计费刷新的最小间隔（秒）：billing/* 连续查询易触发上游拦截，
# 每条消息都刷是流量放大器，与 monitor 轮询共享 last_checked_at 去抖。
BILLING_REFRESH_MIN_INTERVAL = _int("ZCODE_BILLING_REFRESH_MIN_INTERVAL", 60)
# ── 上游错误重试 / 冷却（参数可设定）─────────────────────────────────────────
# 429 频控：账号不冷却，原地等待后重试，耗尽后换下一个账号（账号保持可用）
RETRY_429_TIMES = _int("ZCODE_RETRY_429_TIMES", 5)       # 429 重试次数
RETRY_429_WAIT = _int("ZCODE_RETRY_429_WAIT", 60)        # 429 重试等待秒数（上游 Retry-After 优先）
RETRY_429_WAIT_MAX = _int("ZCODE_RETRY_429_WAIT_MAX", 120)  # Retry-After 采信上限（防吊死客户端）
# 429/code 3009「model concurrency limit exceeded」是并发争用而非频控：上游没有
# Retry-After，等 60s 纯属浪费（槽位早空了）。这类回短退避，配 jitter 打散重试。
RETRY_429_CONCURRENCY_WAIT = _int("ZCODE_RETRY_429_CONCURRENCY_WAIT", 3)
# 429/code 1302/1303「真限流」（Z.ai 频控，magpie 同源实证）：与并发争用相反，
# 原地重试只会加重——账号休息该时长（秒），请求下沉下一渠道/换号
RATE_LIMIT_COOLDOWN = _int("ZCODE_RATE_LIMIT_COOLDOWN", 60)
# Key 通道 1113「无资源包」（永久态）后的选号冷却（秒）：不冷却的话每个请求
# 都会重选该号去撞一次必然失败的 1113（2026-11-11 线上实证：151 个请求 =
# 151 次白烧上游调用）。进程内记忆，重启后自动重探一次
KEY_NO_BALANCE_COOLDOWN = _int("ZCODE_KEY_NO_BALANCE_COOLDOWN", 3600)
# Plan 通道模型级额度耗尽（1005 exceed quota limit）后的选号冷却（秒）：
# 同号不同模型额度独立（旗舰 1005 时 Flash 可能仍有大量余量），不按模型记
# 的话点名单一模型的请求会把全池每号撞一遍 1005 才 503（2026-11-12 线上
# 实证）。TTL 到期或 billing 刷新确认有可用窗口时自动重探/清除
PLAN_MODEL_EXHAUST_COOLDOWN = _int("ZCODE_PLAN_MODEL_EXHAUST_COOLDOWN", 1800)
# 5xx 等一般错误：重试，耗尽后账号冷却 COOLING_SECONDS 并换下一个账号
RETRY_5XX_TIMES = _int("ZCODE_RETRY_5XX_TIMES", 3)       # 5xx 重试次数
RETRY_5XX_WAIT = _int("ZCODE_RETRY_5XX_WAIT", 5)         # 5xx 重试等待秒数
# 限流（cooling）冷却时长（秒）——仅 5xx 重试耗尽 / 连接失败使用
COOLING_SECONDS = _int("ZCODE_COOLING_SECONDS", 300)
# 风控（3012/405「unusual activity」）指数退避冷却：实测为频道级瞬时频控
#（同号同刻 billing 正常、数小时自愈），冷却自动恢复；累计 RISK_BAN_STRIKES
# 次才升级为禁用（人工恢复）。冷却期零上游流量（is_cooling 全通道门禁）。
RISK_COOLDOWN_BASE = _int("ZCODE_RISK_COOLDOWN_BASE", 900)    # 首次冷却秒数
RISK_COOLDOWN_MAX = _int("ZCODE_RISK_COOLDOWN_MAX", 86400)    # 冷却上限（24h）
RISK_BAN_STRIKES = _int("ZCODE_RISK_BAN_STRIKES", 4)          # 窗口内累计命中达到即禁用
RISK_STRIKE_DECAY_SECONDS = _int("ZCODE_RISK_STRIKE_DECAY_SECONDS", 7 * 86400)
# 距上次风控命中超过该窗口则 strikes 重新起算：跨月偶发命中不累积成禁用
RISK_AUTO_ROTATE = _bool("ZCODE_RISK_AUTO_ROTATE", True)
# 风控升级禁用时自动换发设备指纹（新 SKU + 新 device_mid）并后台补跑安装序
# ——「风控后换设备重生」语义自动化，账号重新启用时即全新身份
# 上游读空闲超时（秒）：httpx read 超时是「相邻 chunk 之间」的空闲上限。原为
# read=None——挂死的上游会无限占用账号并发槽 + 闸门槽 + 客户端连接（2026-11-14
# review）。600s 远大于长思考的实际间隙，足以兜底任何真实 stall。
UPSTREAM_READ_TIMEOUT = _int("ZCODE_UPSTREAM_READ_TIMEOUT", 600)

# ── 进程级上游闸门（只作用于 zcode-plan 渠道，见 app/gate.py）────────────────
# 默认关闭速率/在飞两项：多出口/多实例部署下全局速率应由外层网关决定；单出口
# 小池部署可按 magpie 实证打开（Start Plan 约 18 请求/分钟、在飞 ≤ 3~8 更稳）。
GATE_RATE_PER_MIN = _int("ZCODE_GATE_RATE_PER_MIN", 0)     # 每分钟请求预算，0=关闭
GATE_MAX_INFLIGHT = _int("ZCODE_GATE_MAX_INFLIGHT", 0)     # 全池在飞上限，0=关闭
# 边缘 405（IP 封禁）后全进程暂停 plan 渠道的秒数（magpie 实测封禁 1–2 小时，
# 3 分钟只是「先别再撞」；窗口过后请求会重新探测，封禁仍在则再次触发）
GATE_EDGE_HOLD_SECONDS = _int("ZCODE_GATE_EDGE_HOLD_SECONDS", 180)
# 忙碌断路器（busy-hold，Magpie BUSY_N/BUSY_WINDOW/BUSY_HOLD 同款，默认开）：
# 3008 在短退避重试后**仍然** 3008（真饱和；一次重试就过去的瞬态争用不计）
# 在 GATE_BUSY_WINDOW_SECONDS 窗口内累计 GATE_BUSY_THRESHOLD 次，全进程暂停
# plan 渠道 GATE_BUSY_HOLD_SECONDS 秒——封禁前先降速（magpie 实证：重试吸收
# ~90% 的 3008，上层无感不降速，3–7 分钟后地址被封 1–2 小时）
GATE_BUSY_THRESHOLD = _int("ZCODE_GATE_BUSY_THRESHOLD", 5)
GATE_BUSY_WINDOW_SECONDS = _int("ZCODE_GATE_BUSY_WINDOW_SECONDS", 60)
GATE_BUSY_HOLD_SECONDS = _int("ZCODE_GATE_BUSY_HOLD_SECONDS", 180)
# 套餐窗口闸门（pool-hold，2026-11-12）：全池账号都有「过期后实查确认枯竭」
# 证据且无未来窗口时，快速拒绝 messages（不白撞 5 轮账号×验证码）。hold 上限
# 防吊死客户端（无未来窗口时按上限兜底）；0 = 关闭闸门。判定/解除口径见
# models.Account.next_window_start 与 quota.fetch_quota 的恢复分支
GATE_POOL_HOLD_MAX_SECONDS = _int("ZCODE_GATE_POOL_HOLD_MAX", 3600)
# 证据链补查的进程级节流（2.7.31 review 整改）：真空期上游 balances 为空时
# （2026-11-12 线上实锤：22:49 refresh 跑了但 verified_at 没动——quota_map
# 空 → 证据点不打 → 账号永远 stale），若无节流每条请求都会对全池 stale 号
# spawn billing 实查，60s 去抖挡不住「每分钟一轮 × 整个真空期」的持续轰炸
# （fetch_quota 一次 = 3 条 GET，是 monitor 1800s 节奏的 60 倍，vault 明载
# billing 连续查询易触发拦截，403 还会把号标 INVALID）。首探立即、后续每
# 号最多按此间隔重探；balances 一旦列出窗口，一轮实查即闭合证据链
GATE_POOL_PROBE_MIN_SECONDS = _int("ZCODE_GATE_POOL_PROBE_MIN", 300)

# 单账号并发上限。上游 ZCode 单账号并发写死 3（用户 2026-11-13 口径，此前的
# 「2」为当时实测保守值）——本地 cap 高于它只会常态化吃 3008 短退避，故取 3：
# 超限自动切下一账号。不读 ZCODE_ACCOUNT_CONCURRENCY，也不走后台 meta 覆盖
# （2→10→3 纠正：上游 ZCode 单账号并发写死 3，本地闸必须同值，放更宽只会吃 3008）
ACCOUNT_CONCURRENCY = 3
# ── per-account 出口隔离（2.7.36，需求 = 每账号一个独立 IP 互相隔离）──────────
# 依据：①上游按出口 IP 风控（magpie 实证：同 IP 同时用 3 个账号即触发边缘
# 405 封地址，2 个不封——共享出口下 7 号远超红线）；②3008 风暴全池均匀爆发
# = 上游按 IP 维度限并发的实证。开关关闭时全链路走共享出口现状（行为零变化）。
# listener 池 = 逗号分隔的 mihomo listener URL（tebi 上 11031-11037 各钉一个
# pigeonw 节点）；账号入池/启动补分配时领取，随 data blob 持久化。
PER_ACCOUNT_EGRESS = _bool("ZCODE_PER_ACCOUNT_EGRESS", False)
EGRESS_LISTENERS = [u.strip() for u in os.getenv("ZCODE_EGRESS_LISTENERS", "").split(",") if u.strip()]
# 套餐自动领取轮间隔（秒）：周期对全部可打 billing 的 JWT 账号轮一遍
# preview + 领取（有可领套餐才补激活上报，见 claim.auto_claim_all_plans）。
# 默认 3600（2.6.5 整改）：zcode-switch 参照系下 preview/领取只在用户点界面
# 时触发；10 分钟轮次 + 每轮 app_launch 上报是标记不消退的帮凶。0 = 关闭轮次，
# 仅入池/手动触发。运行期可在后台设置改（meta 表即时生效）
CLAIM_ROUND_INTERVAL = _int("ZCODE_CLAIM_ROUND_INTERVAL", 3600)
# 领取窗口（北京时间小时，逗号分隔；空/"*" = 全天）。上游套餐按天投放，
# 用户侧实证每天 19:00 放量——窗口外轮询只会空转 preview。
CLAIM_ROUND_HOURS = os.getenv("ZCODE_CLAIM_ROUND_HOURS", "19")

# ── 上游端点 ─────────────────────────────────────────────────────────────────
# 上游端点：默认值统一收口在 constants.py，环境变量仅作覆盖
UPSTREAM = {
    "zai": os.getenv("ZAI_UPSTREAM_URL", constants.MESSAGES_URLS["zai"]),
    "zai_fallback": os.getenv("ZAI_FALLBACK_URL", constants.MESSAGES_URLS["zai_fallback"]),
    "bigmodel": os.getenv("BIGMODEL_UPSTREAM_URL", constants.MESSAGES_URLS["bigmodel"]),
}

# 跨 provider 降级顺序（Magpie 模式）：当前 provider 池无可用号时按此顺序下沉。
# 首个为默认路由 provider；裸模型名与 zai/ 前缀均落在 providers[0]。
PROVIDER_FALLBACK_ORDER = tuple(
    os.getenv("ZCODE_PROVIDER_FALLBACK_ORDER", "zai,bigmodel").replace(" ", "").split(",")
) if os.getenv("ZCODE_PROVIDER_FALLBACK_ORDER") else ("zai", "bigmodel")

# ZCode 计费 / 额度查询端点
ZCODE_BILLING_BASE = constants.BILLING_BASE
# 官方更新清单端点（测试时指向 Mock 上游）
ZCODE_MANIFEST_URL = os.getenv("ZCODE_MANIFEST_URL", constants.ELECTRON_MANIFEST_URL)
# 官方版本自动探测间隔（秒，默认 12 小时）
VERSION_CHECK_INTERVAL = _int("ZCODE_VERSION_CHECK_INTERVAL", 43200)
# 是否自动将客户端伪装对齐至官方最新版本
AUTO_SYNC_CLIENT_VERSION = _bool("ZCODE_AUTO_SYNC_CLIENT_VERSION", True)
# messages 通道强制直连（2026-11-04 线上实证：env 代理出口 GCP IP 被上游持续
# 标记——messages 走代理 17h+ 零成功（3012 unusual activity），而同账号
# billing/claim/telemetry 直连全部正常。切直连统一全链路出口，消除
# 「CN billing + US messages」双 IP 异常信号；captcha 签发出口由 gateway
# 的同一 egress tag 驱动自动对齐（app/egress.py「token 出口=请求出口」）。
MESSAGES_FORCE_DIRECT = _bool("ZCODE_MESSAGES_FORCE_DIRECT", False)
# 网关密钥未配置时是否放行（默认拒：未配置 = 公开代理，任何人可白嫖账号池）。
# 仅本地开发/测试可显式打开；生产请务必在后台设置 Gateway API Key（2.7.33）。
GATEWAY_ALLOW_NO_KEY = _bool("ZCODE_GATEWAY_ALLOW_NO_KEY", False)
# 前后端分离跨域白名单（2.7.34）：前端部署到独立源（GitHub Pages/CF Pages/独立
# nginx）时填该源，逗号分隔；默认空 = 仅同源（不输出 CORS 头）。鉴权是纯
# Bearer 头（无 cookie），跨域无 CSRF 面。
CORS_ORIGINS = os.getenv("ZCODE_CORS_ORIGINS", "")
# billing/claim 链路出口（2026-11-13 tebi 线上实证）："direct"（历史缺省，
# GCP 类大厂 ASN 直连可用）或 "proxy"（机房小 ASN 直连 IP 被 billing 风控
# 拒——preview 空投放/429、验证码 token 3007；走家宽代理 3/3 号立即可领）。
# 覆盖四处：_billing_request / report_activation（trust_env）、claim 验证码
# egress tag——三者必须同出口，token 与请求 IP 不一致会被判 unusual activity。
BILLING_EGRESS = (os.getenv("ZCODE_BILLING_EGRESS", "direct").strip().lower() or "direct")
if BILLING_EGRESS not in ("direct", "proxy"):
    BILLING_EGRESS = "direct"
# 营销触达 / 活动投放查询端点（测试时指向 Mock 上游）
ZCODE_MARKETING_TOUCH_URL = os.getenv("ZCODE_MARKETING_TOUCH_URL", constants.MARKETING_TOUCH_URL)
# 激活事件上报（测试时指向 Mock 上游）
ZCODE_EVENT_REPORT_URL = os.getenv("ZCODE_EVENT_REPORT_URL", constants.EVENT_REPORT_URL)

# OAuth 与兑换链 origin（测试时指向 Mock 上游）
OAUTH_API_BASE = os.getenv("ZCODE_OAUTH_API_BASE", constants.ZCODE_ORIGIN + "/api/v1")
ZAI_EXCHANGE_ORIGIN = os.getenv("ZCODE_EXCHANGE_ORIGIN", constants.ZAI_API_ORIGIN)

USER_AGENT = os.getenv("UPSTREAM_USER_AGENT", constants.USER_AGENT)
APP_VERSION = "2.7.36"

# 前后端分离（2.7.34）：frontend/version 文件仅作部署簿记（rsync 发版时人工 bump），
# 后端不再读取——版本展示走前端 /meta 拉取，静态资源缓存走 StaticFiles etag 协商。
