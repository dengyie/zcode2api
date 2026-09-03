# 04 — API 规范

状态：Phase 1 端点定稿；Phase 2 端点（OpenAI/Responses）标注草案。

所有管理端点挂 `/admin/api/*`，需 `Authorization: Bearer <后台密码>`；网关端点按「网关 Key」配置可选鉴权（`Authorization: Bearer` 或 `x-api-key`，未配置即放行——生产必须配置）。

## 1. 网关端点（对外）

### 1.1 `POST /v1/messages`（Anthropic Messages，Phase 1）

- 请求/响应：标准 Anthropic Messages API（含 `stream: true` 的 SSE 透传）。
- 上游转发目标由账号模式决定：JWT → `zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages`；API Key → `api.z.ai/api/anthropic/v1/messages`。
- 行为：池内 round-robin 选号 → 单账号失败按分类换号（≤`pool.max_attempts`）→ 验证码挑战原账号重试（≤3）。
- 模型名规范化：小写化后映射（`glm-5.2→GLM-5.2`、`glm-5-turbo→GLM-5-Turbo`、`glm-turbo→GLM-5-Turbo`、`glm-5.1→GLM-5.1`、`glm-4.7→GLM-4.7`）；未知名原样透传。

### 1.2 `GET /v1/models`

```json
{ "object": "list", "data": [ { "id": "GLM-5.2", "type": "model", "display_name": "GLM-5.2" }, ... ] }
```

模型清单来自配置 `models`（默认 `glm-4.5-air, glm-4.6, glm-4.6v, glm-4.7, glm-5, glm-5-turbo, glm-5v-turbo, glm-5.1, glm-5.2, glm-5.3, glm-5.3-flash`）。

### 1.3 `POST /v1/chat/completions`（OpenAI，Phase 2 草案）

- 入站 OpenAI Chat 格式 → 翻译为 Anthropic 上游 → 翻译回 OpenAI 响应；`stream:true` 逐块翻译。
- usage/tool_calls 映射规则以移植对照表为准（`translator/`，见 02 文档 §8）。

### 1.4 `POST /v1/responses`（OpenAI Responses，Phase 2 草案）

- Responses → Chat → Anthropic 链路；`previous_response_id` 用内存 LRU（TTL 24h，重启丢失）。
- 目标客户端：Codex CLI / OpenAI Agents SDK。

### 1.5 `GET /health`

```json
{ "status": "ok", "pool": { "total": 3, "selectable": 2 }, "version": "0.1.0" }
```

### 1.6 错误格式

```json
{ "error": { "type": "<机器可读类型>", "message": "<人读信息>" } }
```

| HTTP | type | 触发 |
|------|------|------|
| 400 | invalid_request_error | JSON/请求体不合法 |
| 401/403 | authentication_error | 网关 key 缺失/不符 |
| 401 | start_plan_jwt_invalid | start-plan JWT 被上游拒绝且池内无可换号 |
| 413 | request_too_large | gzip 解压超限 |
| 500 | captcha_error | 验证码连续求解失败 |
| 502 | upstream_unreachable | 连接级失败（重试后） |
| 502 | translation_failed | 翻译链路失败（Phase 2） |
| 503 | credential_unavailable / no_available_account | 池空 / 全部不可用 |

账号级上游错误在**故障转移耗尽后**回传时：保留上游 status 与 content-type，body 为上游错误原文（转 JSON 失败则 500 字符截断文本）。

## 2. 管理端点（对内）

### 账号池

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/accounts` | 全量列表（`public_view`：脱敏凭证 + 状态 + PlanSlots + 用量） |
| `POST /admin/api/accounts` | `{provider, name, secret}` 直接入池；同凭证幂等 |
| `POST /admin/api/accounts/{id}/toggle` | 启用/禁用 |
| `DELETE /admin/api/accounts/{id}` | 移除 |
| `POST /admin/api/accounts/{id}/refresh` | 立即刷新该账号额度 |
| `GET /admin/api/pool` | 池概览（各状态计数 + selectable 判定） |

### 凭证与登录

| 方法/路径 | 说明 |
|-----------|------|
| `POST /admin/api/oauth/init` | `{provider}` → `{flow_id, authorize_url}`（zai server-mediated；前端展示链接） |
| `GET /admin/api/oauth/poll/{flow_id}` | 轮询授权结果；成功即入池 |
| `POST /admin/api/bundle/export` | `{ids?, password}` → `.zsb` 二进制流（口令 PBKDF2+AES-GCM） |
| `POST /admin/api/bundle/import` | multipart `.zsb` + password → 导入报告（新增/跳过/失败计数） |

### 额度与领取

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/quota` | 全池 QuotaOverview（PlanSlot 分组、reset_at、错峰轮询状态） |
| `GET /admin/api/claim/preview` | 立即拉取当前可领套餐（全部 JWT 账号） |
| `POST /admin/api/claim` | `{account_ids?}` 手动领取；返回 ClaimOutcome[] |
| `GET /admin/api/claim/history` | claim_history 分页 |

### 设置

| 方法/路径 | 说明 |
|-----------|------|
| `GET /admin/api/settings` / `PUT /admin/api/settings` | 网关 key、后台密码、各 interval（改后即生效，落 meta 表） |

## 3. 幂等与并发约定

- 入池按 `{provider}:{credentialString}` 幂等（重复添加返回既有账号）。
- 领取操作同一账号同时只有一个在途（调度器互斥）。
- `POST /admin/api/accounts` 与 OAuth 回调并发入池：以 SQLite 写锁 + upsert 保证一致。
- 网关转发无上游超时（与 ZCode 桌面行为一致）；客户端断开 → 取消上游流（`httpx` stream close）。

## 4. 客户端接入示例

```bash
# Anthropic 兼容（Claude Code 等）
export ANTHROPIC_BASE_URL=http://127.0.0.1:3000
export ANTHROPIC_AUTH_TOKEN=<gateway_key>
npx claude

# OpenAI 兼容（Phase 2）
curl http://127.0.0.1:3000/v1/chat/completions \
  -H "Authorization: Bearer <gateway_key>" -H "Content-Type: application/json" \
  -d '{"model":"glm-4.6","messages":[{"role":"user","content":"hi"}],"stream":true}'
```
