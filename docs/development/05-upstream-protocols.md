# 05 — 上游协议参考

状态：定稿（三个来源项目实测 + 公开配置验证）。所有端点已在 zcode2api / zcode-switch / zcode-api 中观测到真实流量。

## 1. 端点拓扑

```
认证:    chat.z.ai/api/oauth/authorize        (浏览器授权)
         zcode.z.ai/api/v1/oauth/token        (授权码换 access_token)
         zcode.z.ai/api/v1/oauth/cli/init     (CLI 发起, server-mediated)
         zcode.z.ai/api/v1/oauth/cli/poll/{flow_id}
         chat.z.ai/api/oauth/userinfo         (Bearer access_token)
         api.z.ai/api/auth/z/login            (access_token → 业务 JWT)

AI:      zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages   (Plan 通道, Bearer JWT)
         api.z.ai/api/anthropic/v1/messages                   (API Key 通道, x-api-key)
         open.bigmodel.cn/api/anthropic/v1/messages           (BigModel, x-api-key)

计费:    zcode.z.ai/api/v1/zcode-plan/billing/current   ⚠️ WAF
         zcode.z.ai/api/v1/zcode-plan/billing/balance   ⚠️ WAF
         api.z.ai/api/biz/subscription/list             ✅
         api.z.ai/api/monitor/usage/quota/limit         ✅
         zcode.z.ai/api/v1/client/configs               ✅ 免鉴权

领取:    zcode.z.ai/api/v1/zcode-plan/billing/preview
         zcode.z.ai/api/v1/zcode-plan/billing/claim

风控:    zcode.z.ai/api/v1/agent/configs                (endpoint routing + 签名开关)
```

⚠️ WAF：实测会拦截非浏览器特征的 `billing/current|balance` 访问——**错峰轮询 + 完整身仿真头是硬要求**，`fetch_quota` 对 401 需先排除验证码挑战再判凭证失效。

## 2. 认证链路

### 2.1 zai OAuth（CLI server-mediated，headless 友好）

```
1. POST zcode.z.ai/api/v1/oauth/cli/init
   Headers: Authorization: Bearer <本地随机 poll_token>, Content-Type: application/json
   Body:    {"provider": "zai"}
   → data.{flow_id, authorize_url, poll_token?}（2026-09 新协议：服务端下发 poll_token，
     后续 poll 必须采用该值；未下发时回落本地随机值，兼容旧协议）
2. 浏览器打开 authorize_url（chat.z.ai/api/oauth/authorize?client_id=client_P8X5CMWmlaRO9gyO-KSqtg&...；
   新协议下 redirect 直接指向官方 cli callback，客户端原样使用不再改写）
3. GET zcode.z.ai/api/v1/oauth/cli/poll/{flow_id}   (Bearer poll_token，轮询至授权完成)
   → data.accessToken（含过期时间）+ zcodejwttoken（视返回结构）
   poll 4xx 承载 code=3004 → 会话过期（重新发起），其余 4xx → 终态 failed
4. POST api.z.ai/api/auth/z/login  {"token": "<access_token>"}  → 业务 JWT
5. GET  chat.z.ai/api/oauth/userinfo (Bearer access_token)      → user_id
6. 兑换 API Key：getCustomerInfo(默认机构/项目) → api_keys(name="zcode-api-key") → copy/{key} 取 secretKey
```

桌面客户端（对照）：redirect_uri 为 `zcode://zai-auth/callback`，本地 HTTP 服务器接 code 后走 `POST /api/v1/oauth/token`（`{provider:"zai", code, redirect_uri, state}`）。

### 2.2 bigmodel OAuth

```
authorize: https://bigmodel.cn/login?redirect={REDIRECT_ENC}&appId=zcode&state={p}
回调:      zcode://oauth/callback (桌面) / 本地回调端口（CLI 实现）
token 交换: zcode.z.ai/api/v1/oauth/token
业务侧:    bigmodel.cn 域（getCustomerInfo / api_keys），API 形态同 zai
```

## 3. 对话请求（Plan 通道，start-plan 需验证码）

```http
POST https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages
Authorization: Bearer {zcodejwttoken}
anthropic-version: 2023-06-01
Content-Type: application/json
User-Agent: ZCode/{app_version}
X-ZCode-App-Version: {app_version}
X-ZCode-Agent: glm
HTTP-Referer: https://zcode.z.ai/
X-Title: Z Code@{sourceTitle}
X-Device-Mid: {uuidv4，首次生成永久复用}
X-Aliyun-Captcha-Verify-Param: {verifyParam}        # start-plan 必需
X-Aliyun-Captcha-Region: {region}
```

Body：标准 Anthropic Messages（`model/max_tokens/stream/system/messages`）。上游模型名**大小写敏感**（`GLM-5.2`、`GLM-5-Turbo`）。

**Body 形状（网关统一变换，`app/body_transform.py`）**：system 严格三块（cliPrefix / stableSections / dynamic，3.17.2 逐字节，见 `zcode_system.json`）；首条 user 轮前插 `<system-reminder>Today's date is …</system-reminder>`；末条消息最后一块打 `cache_control: ephemeral`；`metadata.user_id` = `{"device_id": mid, "account_uuid": "", "session_id": sid}`。**ToolSearch 别名**：客户端的 Claude Code 会带 ToolSearch 工具（defer_loading 延迟加载），上游见到**原名**会把其余工具全部藏起——网关改名 `ToolSearchTool`、剥 `defer_loading`、丢 `DeferredToolPlaceholder`，响应侧在流式透传中还原回原名（Magpie searchless/searchNamed 同款，api.z.ai 同症全通道生效）。

**attribution 生命周期（会话/轮内稳定，非每请求全新）**：`x-session-id` 与 metadata blob 的 `session_id` 同值，会话内稳定（hub 用「会话首轮文本」派生）；`x-query-id` 由「会话 + 用户轮计数」确定性派生（`conversation_query_id`），一次提问带出的全部请求（含工具循环）同值——工具循环里 N 个请求 N 个 query-id 是可被上游统计的行为指纹。

API Key 通道差异：`x-api-key: {apiKey}.{secret?}` 替代 Bearer，无验证码头。

**验证码 verifyParam**：Node+jsdom 运行阿里云官方无痕 SDK（`AliyunCaptcha.js`，o.alicdn.com），`startTracelessVerification()` 成功回调给出 `verifyParam = base64(JSON{certifyId, sceneId, isSign, securityToken})`；sceneId/region/prefix 从 `client/configs` 动态取（默认 `11xygtvd/sgp/no8xfe`）。挑战形态：403 + captcha 头；或 400 + body `{"code":3007}`。

**被拒信号**（池分类依据）：

| 上游响应 | 判定 | 动作 |
|----------|------|------|
| 402 / body 含 quota|insufficient|balance|exhaust|额度|余额不足 | 额度耗尽 | exhausted，30min 重试窗 |
| 429 + `code` 3008/3009/3010 | 并发已满（用户/模型/轮次，瞬态争用） | **不冷却账号**，短退避 `RETRY_429_CONCURRENCY_WAIT`+jitter 重试；耗尽下沉 Key 渠道。同一请求**连续两次**被拒 = 真饱和信号，`GATE_BUSY_THRESHOLD`（5）次/60s → 全进程 busy-hold `GATE_BUSY_HOLD_SECONDS`（180s，`app/gate.py`）——封禁前先降速 |
| 429 + `code` 1302/1303 | 真限流（Z.ai 频控） | 账号休息 `RATE_LIMIT_COOLDOWN`（默认 60s），请求下沉 Key 渠道/换号；不原地重试 |
| 429 + `1113` / insufficient balance | Key 无资源包（永久态） | 不等待，下沉/换号 |
| 429（其它/无业务码） | 频控 | 不冷却，按 Retry-After（封顶 120s）原地重试 ×5 |
| 405 文案含 `address was blocked` / `network's address` | **边缘封 IP**（出口 IP 级，无 code 3012） | 全进程暂停 plan 渠道 `GATE_EDGE_HOLD_SECONDS`（默认 180s，`app/gate.py`），账号不改状态；本请求下沉 Key 渠道。宽口径对齐 Magpie `EDGE_PAGE`：405 承载的**任何 HTML 页**（含 errors.aliyun.com 跳转）都按边缘页处置 |
| 401 / 403(非验证码) | 凭证失效 | invalid，直到重登 |
| 403 + captcha 挑战 / 400+`code:3007` | 验证码问题 | 刷新 verifyParam 原账号重试 |

429 业务码分流出处：2.7.6/2.7.9 实测 3009（`{"code":3009,"msg":"model concurrency
limit exceeded"}`，判定必须解析业务码——logid 20 位随机数字，裸子串必撞车）；
3008/3010 与 1302/1303 为 2026-11 magpie 插件（`@magpie-community/opencode-zcode-auth`）
同源实证补齐。边缘 405 同出处：同一出口 IP 同时使用 3 个账号即被封地址（2 个不封），
Start Plan 约 18 请求/分钟、10 在飞 → 3008 连发 3–7 分钟后 405 封 IP 1–2 小时；
进程级闸门（速率预算/在飞上限默认关、断路器默认开）见 `app/gate.py` 与 `settings.GATE_*`。

## 4. 免费额度（Start Plan）

`GET zcode.z.ai/api/v1/client/configs?app_version={ver}`（免鉴权）→ `data.configs.startPlanPreview`：

| 模型 | 日额度 |
|------|--------|
| GLM-5.3（旗舰，原名 GLM-5.2，服务端可换 showName） | 3,000,000 tokens/日 |
| GLM-5-Turbo（≈Sonnet 级） | 2,000,000 tokens/日 |

独立计算、每日重置；额度由服务端按账号在调用时授予与扣减。`billing/balance` 返回 `data.balances[]`（`show_name/model/total_units/used_units/remaining_units/expires_at`）——即 PlanSlot 数据源。

## 5. 领取（活动套餐）

```
GET  billing/preview   → data.previews[]: ClaimPlan{plan_id,name,description,priority,grants,grant_items[{name,units,period}]}
                          （活动未上线时 404 —— 属正常态，静默跳过）
                          查询参数: app_version 必带；platform 参数 preview 容忍、client/configs 拒绝（3001）
POST billing/claim     → 头: Bearer JWT + 验证码头 + X-Device-Mid + X-ZCode-App-Version + X-Platform
                          （实测缺版本/平台头即使验证码有效也 3007 —— asar claimManualPlan 头形态）; body: {plan_id}
                          成功 → data.server_time（服务端时钟，秒）+ data.plan.{starts_at,ends_at} 生效窗口（3.11.2 起）
                          失败码: 1003 already_claimed / 1005 quota_exhausted（data.plan.ends_at × 1000 = 名额恢复时间 next_at）
前置: identity.appVersion ≥ 活动要求的最低客户端版本（否则 ineligible）
```

版本口径（单一真相源 `app/constants.py`）：`CLIENT_APP_VERSION="3.14.4"`（官方客户端
现行版实证：ZCode.exe 注册表 DisplayVersion 3.14.4（2026-10-02 本机实测；前值 3.14.3.7762，构建 2026-09-22）；
zcode-switch Windows 路径读注册表本机真实版同值。**活动投放按最低客户端版本门槛
（见上「前置」行），官方升版后钉旧版会 preview 恒空 ineligible——升版先改此常量**）；
无账号路径的 `CLIENT_PLATFORM="darwin-arm64"`（asar `TH()` = `process.platform-process.arch`）。
有账号时 `X-Platform` / preview `platform` 跟该号 DeviceProfile 走（成套桌面 SKU，一号一台），禁止再盖成全局 darwin-arm64。
`USER_AGENT` / `X-ZCode-App-Version` / configs 查询串全部引用版本常量。

## 6. 免费额度以外的两条通道（认知备查）

| 通道 | 计费 | 端点 | 认证 |
|------|------|------|------|
| API Key 通道 | 用户 BigModel/Z.ai API Key 按量 | `open.bigmodel.cn/api/anthropic` / `api.z.ai/api/anthropic` | `x-api-key` |
| Plan 通道（免费 Start + 付费 Coding） | ZCode 账号额度 | `zcode.z.ai/api/v1/zcode-plan/anthropic` | `Bearer JWT` |

## 7. 风控开关（Phase 4 备查，源自 zcode-api 观测）

- **endpoint routing**：`GET agent/configs` → `data.proxyEndpoint.mapping`（coding-plan Anthropic 端点被映射到 `zcode.z.ai/api/v1/ultra[-zai]/...`）；客户端定期拉取并重写上游 URL，fail-open。
- **client signing V4**：`agent/configs` → `data.codingPlanSignature.enable=true` 时，coding-plan 请求需先握手 `{provider}/api/paas/c1f3a7e2/v2/client`，随后每请求附 Ed25519 签名 + PoW 头；连续两次 401 VERIFY 后进入永久 unsigned 旁路（客户端行为）。start-plan / off-peak 永不免签。

## 8. 客户端身份头完整集（仿真必备）

```json
{
  "User-Agent": "ZCode/{appVersion}",
  "X-ZCode-App-Version": "{appVersion}",
  "X-ZCode-Agent": "glm",
  "X-Platform": "{platform}-{arch}",
  "X-Os-Category": "{osCategory}",
  "X-Release-Channel": "stable",
  "X-Client-Language": "{locale}",
  "X-Client-Timezone": "{timezone}",
  "X-Title": "Z Code@{sourceTitle}",
  "HTTP-Referer": "https://zcode.z.ai/",
  "X-Device-Mid": "{稳定 UUIDv4}"
}
```

`appVersion` 必须可打印 ASCII，非法值静默回退默认；`X-Device-Mid` 永不逐请求随机（防指纹抖动）。
