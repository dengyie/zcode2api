# 测试 02 — 单元测试用例清单

命名：`<模块前缀>-<编号>`。P0=发布阻断，P1=高，P2=常规。实现时每个 ID 对应一个 `pytest` 用例（或参数化组），文件放 `tests/unit/`。

## 1. POOL — 账号池（pool.py）

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| POOL-001 | 空池取号报错 | 空 pool → `CredentialUnavailable`，消息含状态摘要 | P0 |
| POOL-002 | round-robin 顺序 | 2 账号取 4 次 → A,B,A,B | P0 |
| POOL-003 | provider 过滤 | zai+bigmodel 混池，`provider="bigmodel"` → 只出 bigmodel | P0 |
| POOL-004 | exhausted 隔离与到期重进 | quota 失败 → 不可选；`exhausted_retry` 窗口过后恢复可选 | P0 |
| POOL-005 | cooling 到期恢复 | 429 → cooling_until 前 `has_selectable=False`，过期后 True | P0 |
| POOL-006 | invalid 永久隔离 | auth_invalid 后任何时刻不可选；重新 `set_pool`（重登语义）恢复 | P0 |
| POOL-007 | reportSuccess 清除瞬态 | exhausted 账号 success 后立即回 active | P0 |
| POOL-008 | 过期凭证跳过 | `expires_at < now` → 不可选，诊断文案含 "expired" | P1 |
| POOL-009 | describePool 文案 | 各状态计数拼装正确（日志/排障依赖） | P2 |
| POOL-010 | 并发取号压力 | 50 并发 `get_credential` 无死锁、无重复游标错乱（结果覆盖所有可选号） | P1 |
| POOL-011 | disabled 账号不参与 | enabled=False → 跳过，重新启用恢复 | P1 |
| POOL-012 | 单账号池 cursor 不越界 | 1 账号连取 → 恒返回该号，cursor 取模正确 | P2 |

## 2. CLASSIFY — 错误分类（classify.py）

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| CLS-001 | 402 → quota | status=402 → `quota` | P0 |
| CLS-002 | 429 → rate_limited | status=429 → `rate_limited` | P0 |
| CLS-003 | 401 → auth_invalid | status=401 → `auth_invalid` | P0 |
| CLS-004 | 403 纯鉴权 → auth_invalid | body 无验证码特征 → `auth_invalid` | P0 |
| CLS-005 | 403 验证码挑战 → None | body 含 "captcha"/"verify token" → `None`（留给验证码重试） | P0 |
| CLS-006 | 403 captcha 挑战头 → None | 有 `X-Aliyun-Captcha-*` 响应头标记 → `None` | P0 |
| CLS-007 | 400 + 额度关键词 → quota | body 含 "insufficient balance"/"额度" → `quota` | P0 |
| CLS-008 | 400 无关键词 → None | 普通参数错误 → `None`（透传客户端） | P0 |
| CLS-009 | 关键词表全覆盖 | 参数化：quota/insufficient/balance/exhaust/额度/余额不足 各自命中 | P1 |
| CLS-010 | SSE 错误跳过 body 探测 | content-type=event-stream → 仅按 status 判定，不 clone body | P1 |
| CLS-011 | 已处验证码挑战后不再判 invalid | `was_captcha_challenge=True` 且终态 403 → `None` | P0 |
| CLS-012 | 500 → None | 服务器内部错误不标记账号 | P1 |

## 3. GATEWAY — 故障转移循环（gateway/anthropic.py）

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| GW-001 | 首号即成功 | Mock ok → 客户端收到透传响应，池内 use_count=1 | P0 |
| GW-002 | 402 → 换号成功 | sequence=quota_exhausted,ok → 最终 200；首号 exhausted | P0 |
| GW-003 | 连续失败至上限 | 5 账号全 exhausted、max_attempts=4 → 503 或最后错误；第 5 号未被尝试 | P0 |
| GW-004 | 全池耗尽 | 1 账号 402，max_attempts=1 → 返回上游 402 原文（status/content-type 保留） | P0 |
| GW-005 | 401 start-plan 且无可换号 | → 401 start_plan_jwt_invalid | P0 |
| GW-006 | 401 但有可换号 | → 换号成功；原号 invalid | P0 |
| GW-007 | 验证码挑战原账号重试 | sequence=captcha_challenge,ok → 同账号第二次成功；验证码缓存被 invalidate | P0 |
| GW-008 | 验证码连续 3 次失败 | → 换号或 500 captcha_error；不进入 invalid | P0 |
| GW-009 | 连接级失败重试 | 首次 connect error、二连成功（Mock 场景 slow/unreachable）→ 200 | P1 |
| GW-010 | postWrite 失败不重发 | ordered transport 场景（Phase 2 前可延后） | P2 |
| GW-011 | 模型名映射 | `glm-5-turbo` → 上游收到 `GLM-5-Turbo`；未知名原样 | P1 |
| GW-012 | 网关鉴权 | 无 key 配置放行；配置后缺/错 key → 401/403 | P0 |
| GW-013 | SSE 透传保真 | 上游 SSE 分块随机切 → 客户端重组后帧序一致（含 usage 帧） | P0 |
| GW-014 | 客户端断开 | 客户端 abort → 上游流被关闭（Mock 记录连接关闭） | P1 |
| GW-015 | 身份头注入 | 上游收到的 User-Agent/X-ZCode-*/Referer/X-Device-Mid 符合 05 文档 §8 | P0 |

## 4. QUOTA — 额度（quota.py）

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| QU-001 | balance 解析 | Mock `billing/balance` balances[] → PlanSlot/QuotaItem 字段齐全 | P0 |
| QU-002 | 全模型归零 → exhausted | remaining 均 ≤0 → 状态 exhausted + last_error | P0 |
| QU-003 | 部分归零不误标 | GLM-5.3 有量、Turbo 耗尽 → 不 exhausted | P0 |
| QU-004 | 恢复自动激活 | exhausted 账号查询到 remaining>0 → 回 active，cooling_until 清空 | P0 |
| QU-005 | 401 与验证码区分 | 401 body 含 "captcha" → 不标 invalid；纯 401 → invalid | P0 |
| QU-006 | startPlanPreview 解析 | `client/configs` → GLM-5.3 3M / Turbo 2M 展示数据 | P1 |
| QU-007 | 错峰轮询 | 5 账号一轮 → 各账号探测起始时间差在 stagger 窗口内 | P1 |
| QU-008 | WAF 拦截退避 | billing 端点 403 → 单账号本轮跳过，不重试风暴 | P1 |
| QU-009 | 多端点并发 | 单账号 3 端点并发（respx 断言并行）且结果归并 | P1 |
| QU-010 | 后台循环 interval=0 关闭 | 配置 0 → 不轮询；改回 >0 即恢复（meta 热生效） | P2 |

## 5. CLAIM — 领取（claim.py）

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| CL-001 | 404 静默等待 | preview 404 → 不记错误、按 poll_interval 继续 | P0 |
| CL-002 | 自动领取成功 | preview 有套餐 + captcha ok → POST claim 携带 Bearer JWT + 验证码头 + X-Device-Mid；claim_history 落库 | P0 |
| CL-003 | already_claimed 退避 | → 等服务端 next window，不消耗 cooldown | P0 |
| CL-004 | quota_exhausted 退避 | 同上语义 | P1 |
| CL-005 | 其它失败 cooldown | → 10min 冷却（注入时钟验证） | P0 |
| CL-006 | ineligible（版本低） | appVersion 低于要求 → 明确失败原因 | P1 |
| CL-007 | 领取互斥 | 50 并发触发同一账号 → 仅 1 个在途请求 | P1 |
| CL-008 | 多账号批量 | 池内 3 JWT 账号 → 各自领取，互不阻塞（错峰） | P1 |
| CL-009 | auto=false 手动模式 | 调度器不自动发 claim；`POST /admin/api/claim` 生效 | P1 |
| CL-010 | 非 JWT 账号跳过 | apiKey 模式账号不参与领取 | P1 |

## 6. CAPTCHA — 验证码（captcha.py / solver.js）

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| CAP-001 | 缓存命中 | TTL 内二次取参 → 不再起子进程 | P0 |
| CAP-002 | 并发单飞 | 20 并发取参 → 仅 1 次求解，其余等锁后命中缓存 | P0 |
| CAP-003 | 失败重试上限 | solver 连续超时 → 第 4 次后抛 captcha_error | P0 |
| CAP-004 | 超时杀进程 | 注入慢 solver → 40s 后 kill、无僵尸进程 | P1 |
| CAP-005 | 动态配置回退 | `client/configs` 失败 → 使用默认 sceneId/region/prefix | P1 |
| CAP-006 | invalidate 生效 | invalidate 后下次取参重新求解 | P0 |
| CAP-007 | solver 桩完备性 | jsdom 桩函数（matchMedia/canvas/WebGL/Worker/OffscreenCanvas）清单断言（防误删） | P2 |

## 7. OAUTH / STORE — 登录与存储

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| OA-001 | zai init/poll 流 | Mock OAuth → flow_id/authorize_url 生成、轮询换 token | P0 |
| OA-002 | 兑换链 | z/login → getCustomerInfo（默认机构/项目选择）→ api_keys 查建 → copy secretKey | P0 |
| OA-003 | bigmodel 授权码流 | Mock 回调 → code 换 token → 入池 | P1 |
| OA-004 | 同凭证重登幂等 | 同 apiKey 二次 login → 不新增账号，凭证刷新，label 可更新 | P0 |
| OA-005 | 新凭证入池置活 | 新账号登录后成为 active | P1 |
| ST-001 | SQLite 往返 | 增删改查 + 重启快照重建（游标位置、状态保留） | P0 |
| ST-002 | WAL 并发写 | 网关状态更新 + 管理操作并发 → busy_timeout 生效无异常 | P1 |
| ST-003 | public_view 脱敏 | 所有对外视图不含完整凭证 | P0 |
| ST-004 | 凭证字段加密落库 | DB 文件 raw 扫描无明文 key/jwt（`ZCODE_MASTER_SECRET` 启用时） | P0 |
| ST-005 | meta 热更新 | 修改 interval/cooldown → 运行中进程下一轮生效 | P1 |

## 8. BUNDLE — .zsb 封包（bundle.py）

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| BN-001 | 往返 | export → import → 凭证集合一致 | P0 |
| BN-002 | 错口令失败 | 错误 password → 解包失败且**无明文残留** | P0 |
| BN-003 | 迭代参数固定 | KDF iters 恒为 100000（互通前提） | P0 |
| BN-004 | 随机性 | 同账号两次导出 → salt/nonce/ciphertext 均不同 | P1 |
| BN-005 | 与 zcode-switch 互通 | zsw 导出的 .zsb 导入成功；我方导出喂给 zsw import（对拍，见测试文档 03） | P0 |
| BN-006 | 篡改检测 | 改动 ciphertext 一字节 → GCM 校验失败拒绝 | P1 |

## 9. ZCLIENT — 客户端融合（Phase 3）

| ID | 用例 | 输入 → 预期 | 优先级 |
|----|------|-------------|--------|
| ZC-001 | enc:v1 解密对拍 | zsw 官方向量（测试文档 03）→ 明文一致 | P0 |
| ZC-002 | enc:v1 加密回读 | encrypt → decrypt 往返；与 Node/Rust 实现互解 | P0 |
| ZC-003 | secret 派生矩阵 | win32/darwin/linux × username 来源顺序 → 派生串正确 | P0 |
| ZC-004 | env 覆盖 | `ZCODE_CREDENTIAL_SECRET` 优先于默认派生 | P1 |
| ZC-005 | 导入入池 | 解密 credentials.json → 提取 zcodejwttoken/apiKey → 入池成功 | P0 |
| ZC-006 | 原子切换 | 切换中断（注入 rename 前崩溃）→ live 文件未被破坏 | P0 |
| ZC-007 | 切换前保全 | 未入库的当前登录自动先快照 → 不丢号 | P0 |
| ZC-008 | 路径穿越防护 | id 含 `../`/非法字符 → 拒绝 | P0 |
| ZC-009 | ZCode 运行中热切换保护 | 检测到客户端运行 → 按 behavior 配置拒绝/杀进程 | P2 |
