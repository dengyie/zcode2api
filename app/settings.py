"""运行期配置：环境变量 + 默认值。

所有可调参数集中在此。账号与凭证不在此处，而是持久化到 data/ 目录（见 store.py）。
"""

from __future__ import annotations

import os
import sys
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


def _float(env_name: str, default: float) -> float:
    try:
        return float(os.getenv(env_name, str(default)))
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
# 请求监控环形日志持久化（默认关 = 仅内存，行为同 2.7.x 及以前）：
# 开启后追加写 DATA_DIR/reqlog.jsonl，启动时回读尾部 ≤KEEP 条，重启不清零。
REQLOG_PERSIST = _bool("ZCODE_REQLOG_PERSIST", False)
REQLOG_PATH = DATA_DIR / "reqlog.jsonl"
# 运维事件日志落盘（2.7.64）：logs.py 全部事件行（ok/info/warn/err/req*）同步
# 追加写 DATA_DIR/opslog.jsonl，结构化字段（_ts 含年/level/tag/msg），日切 +
# .1 轮转保留一代。默认开——排障不再受 out.log 10MB 轮转截断与 ANSI 脏文本
# 干扰（2.7.58-61 领取失败潮跨日排查实战提炼）；关闭 = ZCODE_OPSLOG_PERSIST=0。
OPSLOG_PERSIST = _bool("ZCODE_OPSLOG_PERSIST", True)
OPSLOG_PATH = DATA_DIR / "opslog.jsonl"
# 请求日志 prompt 片段开关（2026-11-21 review P2 隐私面）：req() 缺省不在
# stdout/opslog 落用户对话内容（只落 <N chars> 占位）；本地排障显式置 1 才
# 保留 40 字符片段。
LOG_PROMPT_PREVIEW = _bool("ZCODE_LOG_PROMPT_PREVIEW", False)
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
# 管理面失败锁定是否采信 cf-connecting-ip/x-real-ip 代理头（2026-11-21 review
# P1 信任边界）：true 仅当部署处于可信反向代理之后（CF Tunnel / nginx，由代理
# 覆写真实客户端 IP）；缺省 false = 用 socket 对端地址，直连部署上客户端伪造头
# 无法绕过 admin/gateway key 的失败锁定。tebi canonical 部署（127.0.0.1 + CF
# Tunnel）须显式设 true 才能按真实公网客户端 IP 锁定。
TRUST_PROXY_HEADERS = _bool("ZCODE_TRUST_PROXY_HEADERS", False)

# 后台管理密码默认值，首次启动写入 data/accounts.db，之后以数据库（meta 表）为准。
# 强度闸（2026-11-21 review P1）：缺省 "zcode" 5 字符弱口令 + 失败锁定绕过面 =
# 暴露部署上可被秒猜（export 端点明文导出全部 JWT/apiKey = 整机接管）。env 显式
# 设置也过同款强度校验（与后台 PUT ≥12 字符口径一致）；不达标时生成随机 key
# 打印一次（jupyter token 风格），弱口令永不落库。
_DEFAULT_ADMIN_KEY = os.getenv("ZCODE_ADMIN_KEY", "zcode")


def _admin_key_ok(key: str) -> bool:
    return len(key.strip()) >= 12


if _admin_key_ok(_DEFAULT_ADMIN_KEY):
    DEFAULT_ADMIN_KEY = _DEFAULT_ADMIN_KEY.strip()
else:
    import secrets as _secrets

    DEFAULT_ADMIN_KEY = _secrets.token_urlsafe(18)
    if _DEFAULT_ADMIN_KEY.strip() and _DEFAULT_ADMIN_KEY.strip() != "zcode":
        # 用户显式给了 env 但不达标：说明原因并拒绝采用（静默换随机值会让
        # 运维拿着 env 里的弱 key 登录失败排查半天）
        print(f"[zcode-hub] ZCODE_ADMIN_KEY 强度不足（需 ≥12 字符），已生成随机初始密钥："
              f"{DEFAULT_ADMIN_KEY}", flush=True)
    else:
        print(f"[zcode-hub] 未设置 ZCODE_ADMIN_KEY，随机初始后台密钥（请尽快在后台修改）："
              f"{DEFAULT_ADMIN_KEY}", flush=True)

# ── 验证码 ───────────────────────────────────────────────────────────────────
# 预解 token 池。真浏览器求解重（单枚 10–40s、Chromium 数百 MB），池收敛为 1/2
#（TTL 95s → 稳态约 95s 解一枚，Chromium 占空比 ~10–40%）；回滚 legacy 可调大。
CAPTCHA_POOL_MIN = _int("CAPTCHA_POOL_MIN", 1)        # 目标库存（低于则补）
CAPTCHA_POOL_MAX = _int("CAPTCHA_POOL_MAX", 2)        # 池上限
CAPTCHA_TOKEN_TTL = _int("CAPTCHA_TOKEN_TTL", 95_000) # 单枚 token 最大可用时长（ms；上游实际 ~2min）
CAPTCHA_CONFIG_CACHE_TTL = _int("CAPTCHA_CONFIG_CACHE_TTL", 600_000)  # ms

# 验证码求解（真浏览器跑阿里云官方无痕 SDK，对齐 zcode-switch captcha.js。
# happy-dom 路线 2026-09 起被风控「unusual activity」全拒，solver.js 仅留作
# 回滚：ZCODE_CAPTCHA_SOLVER=legacy）
# slidex（2026-11-16 起，缺省）：playwright + slidex aliyun-captcha-js provider
# （z.ai 站点实测内化的求解内核）。根修 Chrome --proxy-server 不支持带凭证代理
# URL（ERR_NO_SUPPORTED_PROXIES）导致的求解盲区——playwright context 级 proxy
# 原生支持 user:pass@host:port，token 出口 = 请求出口铁律不变。
# pw = 旧 puppeteer 路线（ZCODE_CAPTCHA_SOLVER=pw 回滚用）。
NODE_PATH = os.getenv("ZCODE_NODE_PATH", "node")
CAPTCHA_SOLVER_DIR = ROOT_DIR / "captcha_node"
CAPTCHA_SOLVER_MODE = (os.getenv("ZCODE_CAPTCHA_SOLVER", "slidex").strip().lower() or "slidex")
CAPTCHA_SOLVER_PY = CAPTCHA_SOLVER_DIR / "slidex_runner.py"
CAPTCHA_SOLVER_JS = CAPTCHA_SOLVER_DIR / (
    "solver.js" if CAPTCHA_SOLVER_MODE == "legacy" else "solver_pw.js"
)
# Chromium 可执行文件（solver_pw.js 用；env 可覆盖，缺省按常见路径探测）
CHROMIUM_PATH = os.getenv("ZCODE_CHROMIUM_PATH", "/usr/local/bin/chromium")
CAPTCHA_SOLVE_RETRIES = _int("ZCODE_CAPTCHA_RETRIES", 4)
# 领取链求解重试上限（2.7.68 预算解耦）：messages 池路径保持 CAPTCHA_SOLVE_RETRIES
# （求解是热路径可用性兜底，跑满预算值得）；claim 一小时一轮 × 11 号串行，单号
# 跑满 4×240s ≈ 16 分钟会把整晚窗口吃穿（11-16~11-18 三晚 0 成功的结构性放大器，
# opslog 实测轮内 19:11→20:25 逐号串行）。领取路径缺省 2 次，env 可覆盖。
CAPTCHA_CLAIM_SOLVE_RETRIES = max(_int("ZCODE_CAPTCHA_CLAIM_RETRIES", 2), 1)
# 连续 N 次超时型归因即止损（2.7.68）：DEGRADED 是环境级判定（单次即止），
# 但 DEADLINE / TRACELESS:TIMEOUT / EXC:TimeoutError / SETUP:TimeoutError /
# 求解进程超时这些链式超时形态此前不命中任何止损——每号照样跑满 4×240s
# （11-18 opslog：159 次求解 0 成功，主形态即链式超时）。连续 2 次（换过
# context/进程仍超时）= 环境级失败，第 3 次起不再白烧。1 = 首次超时即止。
CAPTCHA_TIMEOUT_GIVEUP_AFTER = max(_int("ZCODE_CAPTCHA_TIMEOUT_GIVEUP_AFTER", 2), 1)
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
# 并发族重试指数退避：第 n 次重试等 base * 2^(n-1)，封顶此值（2026-11-15 生产
# 数据：935 次重试 1/5→5/5 线性分布说明恒定 3s 有一半在白等——前两次快路径
# 不变，后三次给槽位真正释放的时间，减少空转到耗尽换号）。
RETRY_429_CONCURRENCY_WAIT_MAX = _int("ZCODE_RETRY_429_CONCURRENCY_WAIT_MAX", 30)
# 并发族连败提前止损（2.7.54）：同号连续 N 次并发族 429（重试中途无自愈）=
# 上游排队队列已满，继续耗完 RETRY_429_TIMES 退避序列（3+6+12+24+30 ≈ 75s）
# 是让客户端干等——立即止损换号（store.select 零等待）或切 Key 回退。
# 2 = 第 3 次连败即止损（前两轮保留快路径：单次抖动自愈成本低于换号重连）。
# 0/负 = 关闭（回 2.7.53 旧行为）。
CONCURRENCY_GIVEUP_AFTER = _int("ZCODE_CONCURRENCY_GIVEUP_AFTER", 2)
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
# 限流（cooling）冷却时长（秒）——仅 5xx 重试耗尽使用
COOLING_SECONDS = _int("ZCODE_COOLING_SECONDS", 300)
# 连接失败单独冷却（秒，2026-11-14 per-account 优化）：连接失败是**出口链路
# 瞬态**（mihomo↔家宽线抖动），不是账号故障——300s 的账号级冷却会让高峰期
# 白白少一个号（实测 20 并发下 30 分钟 5 次 → 任意时刻 1-2 个号在空冷却）。
# 60s 足够住宅线抖动恢复；5xx（上游过载）仍走 COOLING_SECONDS。
CONNECT_FAIL_COOLDOWN = _int("ZCODE_CONNECT_FAIL_COOLDOWN", 60)
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

# 选号余额平局带（2.7.53）：相对容差——候选 j 比 k 富出超过此比例才算「显著
# 更富」（带外直接胜出），带内视为同档交还轮换偏移。精确整数比较下快照间
# 必有微小漂移（used 逐请求累加），轮换将永远被余额排序压过（tebi 实证：
# 9 号全在 2.83~2.96 亿档、差距 <5%，连选 8 次恒中同一号）。5% 的取值依据：
# 快照漂移远小于此（单请求用量 ~0.001%），真实档位差远大于此（档外号 94% vs
# 首富 100%）。env 可覆盖：档位分层过细（轮换面变小）时运维可放宽。
# 平局容差带（2026-11-21 review P3：此前裸 float() 是全模块唯一不做容错的数值
# env——脏值 import 期崩启动；负数还会让 store.select 的容差判定语义反转）
SELECT_TIE_TOLERANCE = max(0.0, _float("ZCODE_SELECT_TIE_TOLERANCE", 0.05))
# ── per-account 出口隔离（2.7.36，需求 = 每账号一个独立 IP 互相隔离）──────────
# 依据：①上游按出口 IP 风控（magpie 实证：同 IP 同时用 3 个账号即触发边缘
# 405 封地址，2 个不封——共享出口下 7 号远超红线）；②3008 风暴全池均匀爆发
# = 上游按 IP 维度限并发的实证。开关关闭时全链路走共享出口现状（行为零变化）。
# listener 池 = 逗号分隔的 mihomo listener URL（tebi 上 11031-11037 各钉一个
# pigeonw 节点）；账号入池/启动补分配时领取，随 data blob 持久化。
PER_ACCOUNT_EGRESS = _bool("ZCODE_PER_ACCOUNT_EGRESS", False)
EGRESS_LISTENERS = [u.strip() for u in os.getenv("ZCODE_EGRESS_LISTENERS", "").split(",") if u.strip()]
# 出口供给源（2.7.39 Mode B 灰度）：listeners = 本地 mihomo listener 池（现状）；
# tetherpool = 账号绑定 TetherPool 分配（每账号独立 IP，数据面走 :9101 网关，
# 云端池 1553 us-west 节点）。灰度名单 = 账号名逗号分隔，空 = 全量。
EGRESS_SUPPLY = (os.getenv("ZCODE_EGRESS_SUPPLY", "listeners").strip() or "listeners").lower()
if EGRESS_SUPPLY not in ("listeners", "tetherpool"):
    # 拼错静默回落 listeners = 灰度开关形同虚设（review 2026-11-15）——显式
    # 纠偏 + 告警留痕
    print(f"[settings] 警告: ZCODE_EGRESS_SUPPLY={EGRESS_SUPPLY!r} 非法，回落 listeners",
          file=sys.stderr)
    EGRESS_SUPPLY = "listeners"
TP_API = os.getenv("ZCODE_TP_API", "http://127.0.0.1:9100").strip()
TP_GATEWAY = os.getenv("ZCODE_TP_GATEWAY", "127.0.0.1:9101").strip()
TP_TOKEN_FILE = os.getenv("ZCODE_TP_TOKEN_FILE", "/personal/tebi-zcode-hub/tetherpool_token")
TP_SERVICE = os.getenv("ZCODE_TP_SERVICE", "zcode-hub").strip()
TP_REGION = os.getenv("ZCODE_TP_REGION", "us-west").strip()
TP_TYPE = os.getenv("ZCODE_TP_TYPE", "clash_proxy").strip()
TP_TIER = os.getenv("ZCODE_TP_TIER", "standard").strip()
TP_GRAY_ACCOUNTS = [g.strip() for g in os.getenv("ZCODE_TP_GRAY_ACCOUNTS", "").split(",") if g.strip()]
TP_REBALANCE_DEBOUNCE = _int("ZCODE_TP_REBALANCE_DEBOUNCE", 120)
# 网关级故障熔断（2.7.68）：一次 rebalance 内连续 N 个候选都「探针连接级失败
# （st=0）」= 本地网关（EasyTier→TP 9101）故障而非节点问题——继续 acquire/
# 探针只会全池 churn 并把 egress_node 改写到从未探通的候选（11-18 晚 EasyTier
# 崩溃重启期实测形态）。命中即中止本轮 rebalance 并显式告警。1 = 首个连接失败
# 即熔断（最保守）；0/负 = 关闭（旧行为）。
TP_GATEWAY_FAIL_ABORT = max(_int("ZCODE_TP_GATEWAY_FAIL_ABORT", 2), 0)
# 启动时 TP 孤儿分配对账（2.7.68）：allocs 里 zh-<id> 不在本库账号集合 → 逐个
# release。11-17 实查 27 个分配 16 个陈旧（历史删除账号），删除释放只堵新增
# 不清存量；对账从源头回收占位。默认开。
TP_ORPHAN_RECONCILE = _bool("ZCODE_TP_ORPHAN_RECONCILE", True)
# 周期巡检间隔（2026-11-21 迭代 review P2）：IP-Pool 数据面 auto_rehome / gateway
# persist 随时改写绑定，reconcile 不再是启动单次——共占/漂移存量按此间隔回收。
TP_RECONCILE_INTERVAL = max(_int("ZCODE_TP_RECONCILE_INTERVAL", 1800), 300)
TP_HEALTH_INTERVAL = max(_int("ZCODE_TP_HEALTH_INTERVAL", 300), 30)  # 下限防 0 自旋
# 健康探针连续失败多少轮才 rebalance（2026-11-15 生产数据：03:48/04:13 探针
# 链路秒级抖动各连坐 4/3 个账号齐换绑 = 无谓出口 IP churn）。单轮失败只
# 留痕，连续 N 轮（跨 ≥N 个间隔）仍失败才判定节点真死。
TP_HEALTH_FAIL_THRESHOLD = max(_int("ZCODE_TP_HEALTH_FAIL_THRESHOLD", 2), 1)
# 阿里云验证码 CDN 路由验收（2026-11-18 1117 全灭复盘新增维度）：TP 验收/
# 健康探针此前只测 zcode.z.ai——teleapi 节点对 o.alicdn.com 路由劣化（实测
# 9.9~31.9s/资源，直连 0.15s）照样「JSON-APP 放行」，验证码 SDK 引导/
# traceless 后端签名全部超时 → len-76 无 securityToken 降级 stub（2.7.60
# 拒收）→ popup 兜底渲染不出 → claim 求解全灭。换绑候选与健康判定新增
# alicdn 维度：超阈值/失败 = 拒收该候选（不 ban——路由差≠信誉烂，ban 会
# 白白烧池子）。缺省 5.0s = 实测最慢可用档（3.5~5s）上界：再慢即判不合格。
EGRESS_ALICDN_MAX_SECONDS = _float("ZCODE_EGRESS_ALICDN_MAX", 5.0)
EGRESS_ALICDN_PROBE_TIMEOUT = _int("ZCODE_EGRESS_ALICDN_PROBE_TIMEOUT", 12)
# claim IP 信誉驱动重试（2026-11-17 深度迭代收编）：信誉型失败（403 风控文案/
# 3007/验证码求解失败）换绑重试上限与间隔。间隔给 TP ban/新 allocation 生效
# 留缓冲；求解单次可达分钟级，上限过大会让 claim 轮拖长（见 2.7.50 review）。
CLAIM_EGRESS_RETRIES = max(_int("ZCODE_CLAIM_EGRESS_RETRIES", 2), 0)
CLAIM_EGRESS_RETRY_DELAY = _float("ZCODE_CLAIM_EGRESS_RETRY_DELAY", 20.0)
# 领取轮级 wall-clock 预算（秒，2.7.68）：单号最坏 ≈90 分钟（求解长尾 × 换绑链），
# 11 号串行会把 19-23 点窗口吃穿、轮尾下一拍已出窗。预算到点后剩余账号本轮跳过
# （下拍自然续领），保证轮在窗口内收尾。0/负 = 不限（旧行为）。缺省 2700s（45min）
# = interval 3600s 内留出健康余量。
CLAIM_ROUND_BUDGET = _float("ZCODE_CLAIM_ROUND_BUDGET", 2700.0)
# 领取轮账号并发数（2.7.68）：轮内逐号串行是三天 0 成功的吞吐瓶颈；求解进程级
# 上限（CAPTCHA_MAX_CONCURRENT_SOLVES）与 TP rebalance 互斥（per-account lock）
# 都有自己的全局闸，这里 2 并发只是调度并行度，不会放大浏览器/上游压力。
CLAIM_ROUND_CONCURRENCY = max(_int("ZCODE_CLAIM_ROUND_CONCURRENCY", 2), 1)
# 手动 POST /admin/api/claim 的响应预算（秒，2.7.68）：预算内跑完多少号就返回
# 多少号的结果，剩余账号转后台 job 继续执行（GET /admin/api/claim/{job_id} 续查）。
# 缺省 90s——CF Tunnel 100s 硬超时之内，客户端永不 524。
CLAIM_API_BUDGET = _float("ZCODE_CLAIM_API_BUDGET", 90.0)
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
APP_VERSION = "2.7.74"  # 2.7.74（review 迭代二：affinity 指纹键并入网关凭证哈希（NAT 共享出口防碰撞/防伪造污染）+ add_accounts count 只计真新增 + refresh "all" 字符串判真 + Account._extra 降级保留未知字段（to_persist_dict 回写）+ COOLING+None 防御兜底）  # 2.7.73  # 2.7.73（2026-11-21 review 全量根修：P1×2 auth 信任边界+admin key 强度闸；P2×6 captcha/验证码头剔除+compat 别名还原/tool 合并/空块+审计留痕+store 长连接+opslog 批量 flush+停机收口；P3×10 边界契约生命周期）  # 2.7.72.3→2.7.72.2（claim 链高可用 + 固化守护层）之上：①激活上报瞬态重试 0→3 次（2s/4s 阶梯）②billing GET 瞬态重试 3→5 次、阶梯 2/4/8/15s（08:10-08:13 抖动分钟级 49 条失败实锤）③billing 跟版实弹探针（抬版本打真实 preview 全链，上游拒绝自动回滚）④tier 泄漏启动期巡检⑤updated 语义拆分 messages_updated⑥oauth 集成测试墙钟断言根修  # 2.7.71（高可用迭代）之上：①领取链 billing GET 瞬态重试 2→3 次（2s/4s 阶梯退避，19 点轮实证两连败要等下一窗口 3600s 才补上）②reqlog 逐小时聚合（reqs/成败/p95 t_total/峰值并发/在飞/模型 TOP3）挂 /admin/api/monitoring 的 hourly 字段 ③tebi 侧告警守护 zcode-hub-alert（supervisor 常驻：/meta 失联 2 轮 + opslog 关键模式【人工介入/风控升级禁用/listener 冷却/领取预算耗尽/全池耗尽】→ TG @mangoqwqbot，6h 键控去重，socks5h 10822→11030 双路），详见 vault 告警条目  # 2.7.69（出口供给双根修 + listeners 健康探针）之上：深度 review 根修——listener 探针冷却/复活加状态所有权守卫（INVALID/DISABLED/外部 COOLING 不得被探针洗状态：enabled=True 的风控禁用号会被健康循环纳入，原实现探针连败即覆写 DISABLED→COOLING、恢复即复活=绕过人工恢复不变量；对齐 gateway「不洗」纪律）

# 前后端分离（2.7.34）：frontend/version 文件仅作部署簿记（rsync 发版时人工 bump），
# 后端不再读取——版本展示走前端 /meta 拉取，静态资源缓存走 StaticFiles etag 协商。
