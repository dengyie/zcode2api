# 02 — 移植映射表（源码级）

状态：定稿。每行给出**源文件 + 关键符号**，移植时按此追踪；"改写"列说明语义变化。

约定：`z2a` = zcode2api（Python，AGPL-3.0，可整体复制）；`zsw` = zcode-switch（Rust，MIT，可移植并保留版权注记）；`zapi` = TriDefender/zcode-api（无许可证，**只借鉴设计与协议，不复制代码**）。

## 1. 网关与账号池

| 新模块 | 来源 | 源符号 | 移植方式 | 改写点 |
|--------|------|--------|----------|--------|
| `app/store.py` | z2a `app/store.py` | `Store`、`_init_db`、`select`、`add_account` | 复制 | + 加密字段（见 03 文档）；export 改走 bundle |
| `app/models.py` | z2a `app/models.py` | `Account`、`Status` | 复制 | + `plan_slots: list[PlanSlot]`、`label`、`added_at` |
| `app/pool.py` | z2a `store.select` + zapi `src/auth/manager.ts`（设计） | `Store.select` 轮询游标；`AuthManager.reportFailure/reportSuccess/hasSelectable` | 重写 | 拆出纯逻辑 PoolSelector；三态健康 + 时间窗重进；provider 过滤；`describePool` 诊断文案 |
| `gateway/anthropic.py` | z2a `app/routes/gateway.py` | `messages`、`_try_account`、`MODEL_NAME_MAP` | 复制改写 | 故障转移循环参照 zapi `proxyRequest` 的 attempt 循环结构（attempt ≤ `pool.max_attempts`） |
| `gateway/classify.py` | z2a `_EXHAUST_KEYWORDS`/`_is_captcha_error` + zapi `classifyAccountFailure`（设计） | 关键词表：`quota/insufficient/balance/exhaust/额度/余额不足` | 重写 | 402→quota；429→rate_limited；401→invalid；403 先排除验证码挑战（响应头/`code:3007`/body 含 captcha|verify）再判 quota/invalid；400+关键词→quota；SSE 跳过 |
| `gateway/headers.py` | z2a `app/agent.py` + zapi `identity.ts`（协议） | 透传剔除表 `_DROP_HEADERS`；`User-Agent: ZCode/{ver}`、`X-ZCode-App-Version`、`HTTP-Referer`、`X-Title` | 重写 | + `X-Device-Mid`（UUIDv4 首次生成永久复用，存 config） |
| `app/main.py` lifespan | z2a `app/main.py` + zapi `serve()`（设计） | QuotaMonitor 启动、验证码池预热 | 重写 | + ClaimScheduler 启动 |

## 2. 验证码（整体继承 z2a）

| 新模块 | 来源 | 源符号 | 移植方式 | 备注 |
|--------|------|--------|----------|------|
| `captcha.py` | z2a `app/captcha.py` | `CaptchaManager.get_verify_param/_run_solver/invalidate` | 复制 | 缓存 45s、单飞锁、重试 4、超时 40s |
| `captcha_node/solver.js` | z2a `captcha_node/solver.js` | jsdom 桩（matchMedia/canvas/WebGL/Worker/OffscreenCanvas）+ `startTracelessVerification` | 原样复制 | 输出 `VERIFY_PARAM=...`；阿里云 SDK 从 o.alicdn.com 加载 |
| 动态验证码配置 | z2a `fetch_config` + zsw `claim.rs CaptchaConfig` | `GET zcode.z.ai/api/v1/client/configs` → `configs.captcha.{sceneId,region,prefix}`（默认 `11xygtvd/sgp/no8xfe`） | 合并 | 配置缓存 10min |

## 3. 额度模型（zsw quota.rs → Python）

| 新模块 | 来源（zsw `src-tauri/src/quota.rs`） | 源符号 | 改写点 |
|--------|--------------------------------------|--------|--------|
| `models.PlanSlot/QuotaItem/QuotaOverview` | L203 `QuotaItem`、L224 `PlanSlot`、L243 `QuotaOverview` | dataclass 直译 | + `reset_at` 序列化、`account_id` 外键 |
| 多令牌候选 | L293 `candidate_tokens(creds, config, secret)` | 直译 | 我们场景令牌来源不同：池内 Account 已拆好 jwt/api_key，无需从客户端文件反推；仅 `zclient` 导入路径使用 |
| 多端点探测 | L445 `query_quota(tokens)`；URL 常量 L20-L22：`open.bigmodel.cn/api/monitor/usage/quota/limit`、`open.bigmodel.cn/api/biz/subscription/list`、`zcode.z.ai/api/v1/zcode-plan/billing/balance` | 重写为 httpx 并发 + respx 可测 | 单账号内 3 端点并发，账号间错峰 |
| 套餐分层 | L1021 `extract_plan_tier(current_data)` | 直译 | coding-plan / start-plan 分组展示 |
| 计划信息 | `GET zcode.z.ai/api/v1/client/configs?app_version=...` → `data.configs.startPlanPreview`（免鉴权） | 沿用 z2a 免费额度文档结论 | GLM-5.3 3M tokens/日 + GLM-5-Turbo 2M tokens/日，日重置 |

## 4. 活动领取（zsw claim.rs → Python）

| 新模块 | 来源（zsw `src-tauri/src/claim.rs`） | 源符号 | 改写点 |
|--------|--------------------------------------|--------|--------|
| 常量 | L9-L11：`billing/preview`、`billing/claim`、`client/configs` | 原样 | |
| 数据模型 | L38 `ClaimPlan`（plan_id/name/description/priority/grants/grant_items）、L49 `ClaimGrant`、L58 `ClaimOutcome` | dataclass 直译 | + 入库 `claim_history` |
| 领取令牌 | L60 `claim_token(creds, config, secret)`：优先 `zcodejwttoken`，回退 `zai_billing_token` | 简化 | 池内 Account.jwt 直接可用 |
| 领取请求 | POST `billing/claim`，头：`Authorization: Bearer {jwt}` + 验证码头 + `X-Device-Mid` + 版本身份头 | 重写（httpx） | 验证码走 `captcha.py` |
| 退避策略 | `already_claimed`/`quota_exhausted` → 服务端窗口；其它 → cooldown 10min；404 预览未上线静默 | 直译 | 参数入 settings：`claim.poll_interval=300s`、`claim.cooldown=600s`、`claim.auto=true` |
| 参考（协议已验证的实现） | zapi `src/claim/*`（scheduler/runtime/client） | 仅协议对照 | 不复制代码 |

## 5. 加密与封包

| 新模块 | 来源 | 源符号 | 移植方式 | 备注 |
|--------|------|--------|----------|------|
| `zclient.enc_v1` | zsw `src-tauri/src/zcrypto.rs` | `PREFIX="enc:v1:"`、`derive_key`=SHA256(secret)、`decrypt_with_secret`（nonce.tag.ct，URL_SAFE_NO_PAD）、`default_secret`=`zcode-credential-fallback:{node_platform}:{home}:{username}` | Python cryptography 库重写 | 平台映射 win32/darwin/linux（`node_platform_for` L15）；`ZCODE_CREDENTIAL_SECRET` env 覆盖；**必须过对拍向量**（测试文档 03） |
| `zclient.snapshot/switch` | zsw `src-tauri/src/store.rs` | `Paths`（L42-L73：`~/.zcode/v2/{credentials,config,telemetry-state}.json`、账号库 `~/.zcode-switch/accounts/`）、`atomic_write`（L196）、`read_live/write_live`（L206-L241）、`is_logged_in`（L185） | 重写 | 原子写=临时文件+rename；切换前保全当前登录；路径白名单 `[A-Za-z0-9-]` 防穿越 |
| `bundle.py` | zsw `src-tauri/src/cipher.rs` | `KDF_ITERS=100_000`、`pbkdf2_hmac::<Sha256>`、AES-256-GCM、envelope 含 `kdf:{algo,iters,salt}` 字段（L40） | Python 重写 | **格式目标：.zsb 互通**。envelope 完整字段清单在移植首日从 cipher.rs 全文确认并回填到 03 文档；互通用例用 zsw 导出的真实 .zsb 文件验证 |

## 6. OAuth

| 新模块 | 来源 | 源符号 | 备注 |
|--------|------|--------|------|
| zai server-mediated 流 | z2a `app/oauth.py` | `ZaiAuthFlow.init/poll`（`POST /oauth/cli/init`、`GET /oauth/cli/poll/{flow_id}`）+ `exchange_api_key`（z/login → getCustomerInfo → api_keys → copy secretKey） | 原样保留，headless 友好 |
| bigmodel 授权码流 | zsw `src-tauri/src/oauth.rs` L24/L271-L276（协议）+ z2a 无此流程 | authorize URL：`https://bigmodel.cn/login?redirect=...&appId=zcode&state=...` | Phase 1 末补齐；测试用 Mock 上游 |
| token 交换链 | z2a `resolver` + zsw `oauth.rs` | `POST zcode.z.ai/api/v1/oauth/token`（provider/code/redirect_uri/state）→ `POST api.z.ai/api/auth/z/login` | 协议细节见 05 文档 |

## 7. 管理 API / UI

| 新模块 | 来源 | 说明 |
|--------|------|------|
| `routes/admin_api.py` | z2a 同名文件 | 扩展：`/admin/api/quota`（QuotaOverview）、`/admin/api/claim/*`（preview/claim/history）、`/admin/api/accounts/{id}/export`（.zsb） |
| `web/` | z2a `app/statics/` | 复用骨架与鉴权；新增额度面板（PlanSlot 分组 + reset 倒计时）与领取页 |
| 网关鉴权 | z2a `auth_admin.py` | 不变：后台 Bearer + 可选网关 key（`x-api-key`/Bearer） |

## 8. Phase 2 翻译层（zapi 设计参照）

| 新模块 | zapi 参照（设计，不复制代码） | 说明 |
|--------|-------------------------------|------|
| `translator/openai_to_anthropic.py` | `src/translator/openai-to-anthropic.ts` 的映射规则 | 请求/响应/usage 字段映射 + 工具调用 |
| `translator/sse.py` | `src/translator/sse-translator.ts` | 逐块 SSE 双向翻译；`[DONE]`、usage 帧、content_block_delta 映射 |
| `gateway/responses.py` | `src/proxy/responses-handler.ts` 的端点行为 | Responses→Chat→Anthropic 链路；`previous_response_id` 内存 LRU（TTL 24h） |
| 模型白名单 | zapi `config.models` | `glm-4.5-air … glm-5.3-flash`，`defaultModel: glm-4.6` |
