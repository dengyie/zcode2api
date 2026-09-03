# 测试 04 — 集成与 E2E 用例

前置：Mock 上游（`tests/mock_upstream/`）已启动；被测进程以真实运行形态拉起（uvicorn / compose）。用例 ID 前缀：INT（集成）/ E2E（全栈）。

## 1. 集成用例（真实进程 + Mock 上游）

### INT-A 池生命周期

| ID | 场景 | 步骤 | 断言 |
|----|------|------|------|
| INT-001 | 冷启动→登录→服务 | 空 DB 启动 → CLI login（Mock OAuth）→ `/health` 池=1 → `/v1/messages` 200 | 全链路状态一致 |
| INT-002 | 多池轮换分布 | 入池 3 号，连发 9 请求（scenario=ok）→ Mock 记录三号各被用 3 次（round-robin 均匀） | 均匀性 ±1 |
| INT-003 | 故障转移串联 | 号1 402 → 号2 429 → 号3 200：`x-mock-sequence` 按账号维度注入 → 200，且池状态 exhausted/cooling/active 各就位 | 状态机与响应正确 |
| INT-004 | 全池耗尽恢复 | 全部 402 → 后续 503 → Mock 恢复 ok + 注入时间快进（或 exhausted_retry 调小）→ 自动恢复服务 | 自愈 |
| INT-005 | 管理操作热生效 | 运行中改 cooldown/gateway key（admin API）→ 下一请求即生效 | meta 热载 |
| INT-006 | 重启快照恢复 | 停进程 → 重启 → 池状态/游标/统计与停前一致 | 持久化 |

### INT-B 额度与领取

| ID | 场景 | 步骤 | 断言 |
|----|------|------|------|
| INT-010 | 错峰轮询节奏 | 5 号池，缩短 refresh_interval=2s → Mock 记录各账号首查时间戳互差 ≥ stagger 最小值 | 无齐发 |
| INT-011 | WAF 拦截降级 | billing/balance 恒 403 → 该号额度面板显示 unknown 而非 0；不影响网关转发 | 降级不误杀 |
| INT-012 | 领取全流程 | preview 出现套餐 → auto claim（FakeCaptcha 固定 verifyParam）→ Mock 断言 claim 请求头/body → claim_history 有记录、UI 可见 | 端到端 |
| INT-013 | 退避窗口 | claim 返回 already_claimed + next_window=2s → 注入时钟验证恰好在窗口后重试 | 精确退避 |

### INT-C 凭证与封包

| ID | 场景 | 步骤 | 断言 |
|----|------|------|------|
| INT-020 | .zsb 导入→服务 | zsw 生成向量包导入 → 池内账号即刻可服务请求 | 互通即用 |
| INT-021 | 导出→再导入 | 导出全池 → 清库 → 导入 → 请求分布与原池一致 | 往返等价 |
| INT-022 | OAuth 后台化登录 | admin API `/login/start` → 模拟授权回调 → `/login/poll/{fid}` 返回 ready → 池新增；重复 poll 得 `{"status":"expired"}`（会话已摘除，不重入兑换链）；未知/超时 flow_id 同样 expired | UI 流程可用 |

### INT-D 验证码

| ID | 场景 | 步骤 | 断言 |
|----|------|------|------|
| INT-030 | 真实 solver 冒烟 | （可选，`@pytest.mark.real_captcha`）真实 jsdom 子进程求解一次 | 输出 verifyParam 结构合法（base64(JSON{certifyId,...})）；默认 CI 跳过 |
| INT-031 | solver 缺失报错 | 未 npm install → 明确报错指引用，而非悬死 | 用户体验 |

## 2. 故障注入矩阵（Mock 上游 `x-mock-scenario`）

| scenario | 行为 | 主要消费者 |
|----------|------|-----------|
| `ok` | 正常 200（SSE 或批量按请求 stream 参数） | 基线 |
| `quota_exhausted` | 402 + `{"error":{"message":"insufficient balance"}}` | CLS/GW |
| `quota_exhausted_400` | 400 + `{"code":1002,"message":"额度已用完"}` | CLS-007 |
| `rate_limited` | 429 + Retry-After | CLS-002 |
| `auth_invalid` | 401 纯 JSON 错误 | CLS-003 |
| `captcha_challenge` | 403 + 验证码响应头 | CLS-005/006 |
| `captcha_3007` | 400 + `{"code":3007}` | in-body 挑战 |
| `captcha_loop` | 每次都挑战 | GW-008 上限 |
| `connect_fail_first` | 首次真断连（连接上无任何响应字节，客户端 httpx 抛 ReadError/RemoteProtocolError），之后正常 | GW-009 |
| `slow_first_byte` | TTFB 延迟 30s | 超时/取消路径 |
| `sse_truncate` | SSE 中途断流 | GW-013/014 |
| `waf_block` | billing 端点 403 HTML | INT-011 |
| `server_error` | 500 随机 JSON | CLS-012 |
| `garbage_body` | 200 但 body 非 JSON | 解析器健壮性 |

序列注入：`x-mock-sequence: scenario1,scenario2,...` 按请求次数依序消费，`ok` 后续保持。账号维度注入：`x-mock-bind: <api_key前缀>` 让某 scenario 只对指定凭证生效（多号场景必需）。

## 3. E2E 用例（compose 全栈）

环境：`tests/e2e/compose.yaml` = zcode-hub 服务 + Mock 上游 + 种子脚本（预置 3 账号）。

| ID | 场景 | 步骤 | 断言 |
|----|------|------|------|
| E2E-001 | 三客户端协议 | Claude Code 形态（Anthropic SSE）、OpenAI SDK 形态（chat/completions，Phase 2）、Codex 形态（/v1/responses，Phase 2）各发 1 流式请求 | 三协议均 200 且内容一致（同一 Mock 模型输出） |
| E2E-002 | 额度用尽自动降级 | 种子把号1/号2 置 exhausted → 请求自动走号3 → Mock 记录命中 | 故障转移跨进程 |
| E2E-003 | 后台操作流 | UI 登录 → 加号 → 看额度面板 → 手动 claim → 导出 .zsb → 清库导入 | 管理面闭环 |
| E2E-004 | 容器重建恢复 | compose down → up（卷保留）→ 池/设置/历史完好 | 持久化 |
| E2E-005 | 管理鉴权边界 | 未带后台 key 访问 /admin/api/* 全 401；网关 key 未配置时 /v1/* 放行（仅限注入 127.0.0.1 场景） | 安全默认 |

## 4. 真实上游冒烟（发版前，手动）

脚本 `tests/smoke/real_upstream.sh`（需 `ZCODE_SMOKE_CREDENTIALS` 注入，跑完清理）：

1. `GET /health` 池 ≥1
2. `/v1/messages` 8-token 非流式一条 → 200 且 usage>0
3. `/v1/messages` 流式 3 帧 → 客户端重组正常
4. `GET /admin/api/quota` → 各号 PlanSlot 非空
5. `GET /admin/api/claim/preview` → 仅读
6. （可选人工）真实领取一次

红线：不并发、不重试轰炸、单次冒烟 ≤ 6 个上游请求。
