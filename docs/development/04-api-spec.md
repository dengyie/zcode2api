# 04 — API 规范

状态：与 2.5.11 实现对齐。对外网关为 Anthropic Messages、OpenAI Chat Completions、OpenAI Responses 三协议网关 + `/v1/models`，均已实现。

所有管理端点挂 `/admin/api/*`，需 `Authorization: Bearer <后台密码>`（连续失败达上限后 429 锁 5 分钟）；网关端点按「网关 Key」配置可选鉴权（`Authorization: Bearer` 或 `x-api-key`，未配置即放行——生产必须配置）。

## 1. 网关端点（对外）

### 1.1 `POST /v1/messages`（Anthropic Messages，Phase 1）

- 请求/响应：标准 Anthropic Messages API（含 `stream: true` 的 SSE 透传）。
- 上游转发目标由账号模式决定：JWT → `zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages`；API Key → `api.z.ai/api/anthropic/v1/messages`。
- 行为：池内 round-robin 选号（`store.select`）→ 单账号失败按分类换号（≤`MAX_ACCOUNT_ATTEMPTS=5`，满号跳过不计）→ 验证码挑战原账号重试（≤3）。429/5xx/验证码等待期间释放该账号并发槽，醒后重新占槽或换号。
- 模型名规范化：小写化后映射（`glm-5.2→GLM-5.2`、`glm-5-turbo→GLM-5-Turbo`、`glm-turbo→GLM-5-Turbo`、`glm-5.1→GLM-5.1`、`glm-4.7→GLM-4.7`）；未知名原样透传。

### 1.2 `GET /v1/models`

```json
{ "object": "list", "data": [ { "id": "GLM-5.3-Flash", "type": "model", "display_name": "GLM-5.3-Flash", "created_at": "…" }, ... ] }
```

模型清单来自 `constants.AVAILABLE_MODELS`（按账号实际余额窗口公布，当前 `GLM-5.3-Flash` / `GLM-5.3`）。

### 1.3 `POST /v1/chat/completions`（OpenAI 兼容）

- 入站 OpenAI Chat 格式 → 翻译为 Anthropic 上游 → 翻译回 OpenAI 响应；`stream:true` 逐块翻译。
- usage/tool_calls 映射规则以移植对照表为准（`openai_compat.py`）。

### 1.4 `POST /v1/responses`（OpenAI Responses 协议）

- 入站 OpenAI Responses 格式 → 翻译为 Anthropic 上游（`responses_to_anthropic`）→ 翻译回 Responses 对象（`anthropic_to_responses`）；`stream:true` 经 `ResponsesStreamConverter` 逐事件翻译。翻译器实现见 `app/responses_compat.py`，会话状态见 `app/response_store.py`。
- 请求字段映射：
  - `instructions` → `system`；`input` 条目 `message` / `reasoning` / `function_call` / `function_call_output` → Anthropic 对应消息与块（`reasoning` 回放为 thinking，工具调用对 → `tool_use` / `tool_result`）。
  - `tools` 仅接受 `function` 类型（`web_search` 等其他工具类型跳过）；`tool_choice` 映射同 chat/completions。
  - `reasoning.effort` → 上游思考档位：`minimal` / `low` → `low`，`high` → `high`，`xhigh` → `max`，`medium`（或缺省）→ 不携带该字段（上游默认档）。
  - `max_output_tokens` → `max_tokens`；`model` 规范化同 1.1。
  - 未支持字段（`store` / `include` / `metadata` / `prompt_cache_key` / `text` 等）安静忽略。
- 会话续接：`previous_response_id` 命中 → 上一轮完整输入输出拼入上游上下文；存储为进程内存 LRU（256 条，重启清零），未命中返回 400。
- 响应映射：thinking 思考以 `reasoning` 条目（`summary` 内 `summary_text`）回传；`stop_reason=max_tokens` → `status: "incomplete"`，其余 → `"completed"`。

```json
{ "id": "resp_…", "object": "response", "status": "completed", "model": "GLM-5.3-Flash", "output": [ { "type": "reasoning", "summary": [ { "type": "summary_text", "text": "…" } ] }, { "type": "message", "role": "assistant", "content": [ { "type": "output_text", "text": "…" } ] } ] }
```

- 流式为 Responses SSE（`event:` + `data:` 帧，帧内含 `sequence_number`），无 `[DONE]` 哨兵，以 `response.completed` 收尾。事件序：
  - `response.created` → `response.in_progress`
  - 思考：`response.output_item.added` → `response.reasoning_summary_part.added` → `response.reasoning_summary_text.delta`
  - 正文：`response.output_item.added` → `response.content_part.added` → `response.output_text.delta` → `response.output_text.done`
  - 工具：`response.function_call_arguments.delta` → `response.function_call_arguments.done`
  - 收尾：`response.completed`
- 网关 Key 鉴权、故障转移与错误处理与 `/v1/messages` 一致（错误格式见 §1.6）。

### 1.6 错误格式

```json
{ "error": { "type": "<机器可读类型>", "message": "<人读信息>" } }
```

| HTTP | type | 触发 |
|------|------|------|
| 400 | invalid_request / invalid_request_error | JSON 非法，或请求体不是对象 |
| 400 | invalid_request | `previous_response_id` 未命中（会话 LRU 无该记录） |
| 401/403 | （FastAPI HTTPException） | 网关 key 缺失/不符 |
| 500 | captcha_error | 验证码求解失败 |
| 500 | internal_error | 调度层未捕获异常（监控条目会收口） |
| 502 | upstream_error | 上游响应无法读取或格式异常 |
| 503 | no_available_account | 池空 / 全部不可用 / 并发已满 |

账号级上游错误在**故障转移耗尽后**回传时：保留上游 status 与 content-type，body 为上游错误原文（转 JSON 失败则 500 字符截断文本）。

## 2. 管理端点（对内，均挂 `/admin/api/*` 且需后台密钥）

### 账号池

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/accounts` | 全量列表（`public_view`：脱敏凭证 + 状态 + 配额/套餐 + 用量）+ 概览统计 |
| `POST /admin/api/accounts` | `{provider, tokens: [..], name?}` 批量入池（也接受多行字符串）；同凭证幂等；真新增触发安装序 + JWT 自动领取 |
| `PUT /admin/api/accounts/{id}` | 改备注 / 换凭证（按形态自动判 jwt/apiKey） |
| `POST /admin/api/accounts/{id}/enabled` | `{enabled}` 启用/禁用 |
| `DELETE /admin/api/accounts` | body `[ids]` 批量移除 |
| `POST /admin/api/accounts/refresh` | `{all?}` 或 `{ids}` 批量刷新额度（冷却/失效跳过 billing） |
| `POST /admin/api/accounts/{id}/refresh` | 立即刷新该账号额度 |
| `POST /admin/api/accounts/{id}/fingerprint/rotate` | 换发客户端指纹（下一套成套桌面 SKU + 新 device_mid） |
| `GET /admin/api/status` | provider 列表 + 网关 key 是否已配置 + 可选账号计数 |

### 凭证与登录

| 方法/路径 | 说明 |
|-----------|------|
| `POST /admin/api/login/start` | `{label?}` → `{flow_id, authorize_url, expires_in:300}`（zai server-mediated；前端展示链接，`label` 作账号名入池） |
| `GET /admin/api/login/poll/{flow_id}` | 轮询：`{status}` ∈ `pending`/`ready`/`failed`/`expired`；`failed` 附 `message`；`ready` 附 `account`（JWT 已入池；API Key 兑换/额度刷新后台回填）。官方 poll HTTP 4xx → `failed` 并摘会话；5xx/网络抖动 → `pending` 并打日志。ready/失败后或未知/超时 flow_id 一律 `expired`（会话一次性，防重入兑换链） |

### 凭证导入 / 导出

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/export` | 明文 JSON（name/mode/secret；仅后台密钥保护，安装字段不出网） |
| `POST /admin/api/import` | export 同构 JSON；新账号触发安装序 + JWT 自动领取 |

### 额度与领取

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/claim/preview` | 立即拉取当前可领套餐（`?account_id` 单账号；先上报激活事件） |
| `POST /admin/api/claim` | `{account_ids?, plan_id?}` 自动领取（服务端求解验证码，3007 换码重试一次） |
| `POST /admin/api/claim/manual` | `{account_id, captcha_verify_param, captcha_region?, plan_id?}` 浏览器滑块人工领取 |
| `GET /admin/api/claim/captcha-config` | 前端滑块 SDK 初始化参数（scene/region/prefix/enabled） |

### 监控与设置

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/monitoring` | 内存环形请求日志（最新在前，KEEP=500，重启清零） |
| `POST /admin/api/monitoring/clear` | 清空监控 |
| `GET /admin/api/settings` | 不回明文密钥。返回 `admin_key_set` / `admin_key_masked` / `admin_key_is_default`、`gateway_key_set` / `gateway_key_masked`、`quota_refresh_interval`、`account_concurrency` |
| `PUT /admin/api/settings` | 改密钥、刷新间隔、并发上限（改后即生效，落 meta 表；并发 0 = 不限）。前端回填的掩码（含 `…` 或 `••••`）忽略不覆盖；网关 key 传空字符串表示关闭校验 |

### 探活

| 方法/路径 | 说明 |
|-----------|------|
| `GET /meta` | `{version}`（无鉴权，supervisor/部署脚本探活用） |

## 3. 幂等与并发约定

- 入池按 `{provider}:{credentialString}` 幂等（重复添加返回既有账号）。
- 领取操作同一账号同时只有一个在途（调度器互斥）。
- 请求调度单账号并发上限默认 2（`account_concurrency` 设置，0 = 不限）；满号不排队，直接跳下一个可选账号，全部满/无号返回 503。
- `POST /admin/api/accounts` 与 OAuth 回调并发入池：以 SQLite 写锁 + upsert 保证一致。
- 网关转发无上游超时（与 ZCode 桌面行为一致）；客户端断开 → 取消上游流（`httpx` stream close）。

## 4. 客户端接入示例

```bash
# Anthropic 兼容（Claude Code 等）
export ANTHROPIC_BASE_URL=http://127.0.0.1:3000
export ANTHROPIC_AUTH_TOKEN=<gateway_key>
npx claude

# OpenAI 兼容
curl http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer <gateway_key>" -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3","messages":[{"role":"user","content":"hi"}],"stream":true}'

# OpenAI Responses 兼容（非流式）
curl http://127.0.0.1:3000/v1/responses \
  -H "Authorization: Bearer <gateway_key>" -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]}'

# OpenAI Responses 兼容（流式）
curl http://127.0.0.1:3000/v1/responses \
  -H "Authorization: Bearer <gateway_key>" -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}],"stream":true}'
```
