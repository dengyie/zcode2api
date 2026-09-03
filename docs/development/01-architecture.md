# 01 — 总体架构

状态：定稿（v1，随 Phase 1 开工修订）

## 1. 系统定位

zcode-hub 是一个**自托管服务**，同时承担两个角色：

1. **API 供给端（网关）**：把池内 ZCode 账号的 Coding Plan / Start Plan 额度，以标准 API（Anthropic Messages / OpenAI Chat / OpenAI Responses）暴露给任意 agent（Claude Code、Codex CLI、Cline 等）。
2. **账号运营端（控制台）**：账号池的增删、额度监控、限时套餐自动领取、加密封包导入导出、以及（桌面场景）对本机 ZCode 客户端登录身份的快照切换。

```
                    ┌────────────────────────────────────────────────┐
   Claude Code ────▶│  Gateway  /v1/messages  /v1/chat/completions   │
   Codex CLI  ─────▶│           /v1/responses  /v1/models            │
   OpenAI agent ───▶│       （账号池轮换 + 故障转移 + 流式透传/翻译）  │
                    └───────────────┬────────────────────────────────┘
                                    │
┌──────────────┐    ┌───────────────▼────────────────────────────────┐
│ 管理后台 Web │───▶│                FastAPI 核心进程                 │
│ /admin/*     │    │  Store(SQLite) · Pool · Quota · Claim · OAuth  │
└──────────────┘    │  CaptchaManager(jsdom) · Bundle · ZClient      │
                    └───┬──────────────┬───────────────┬─────────────┘
                        │              │               │
                 zcode.z.ai        api.z.ai /     ~/.zcode/v2/*
                 (Plan 通道+       open.bigmodel.cn （本机 ZCode 客户端，
                  验证码/领取)      (API Key 通道)   可选：快照/切换)
```

## 2. 技术栈

| 层 | 选型 | 说明 |
|----|------|------|
| 运行时 | Python 3.12+ | 继承 zcode2api 底座 |
| Web | FastAPI + Uvicorn | 异步网关，SSE 流式透传 |
| HTTP 客户端 | httpx | 连接池 + 流式 + 超时细粒度控制 |
| 存储 | SQLite (WAL) | 账号池 / 设置 / 领取历史；单机自托管 |
| 验证码 | Node + jsdom 子进程 | 复用 zcode2api 方案：无浏览器运行阿里云无痕 SDK |
| 前端 | 原生 JS + 轻量模板 | 继承 zcode2api 后台骨架，扩额度/领取面板 |
| 测试 | pytest + pytest-asyncio + respx | 单元 + 契约；Mock 上游见测试文档 |
| 部署 | Docker / docker-compose；裸机用 supervisor | tebi 容器无 systemd，用 supervisor 约定 |

## 3. 模块清单与职责

```
zcode-hub/
├── app/
│   ├── main.py            # FastAPI 工厂 + lifespan（启动 QuotaMonitor / ClaimScheduler / 预热验证码池）
│   ├── settings.py        # 环境变量 + YAML 配置（沿用 zcode2api .env 风格）
│   ├── models.py          # Account、Status 状态机、PlanSlot/QuotaItem 额度模型
│   ├── store.py           # SQLite 账号池：CRUD、轮询游标、设置 KV、领取历史
│   ├── pool.py            # PoolSelector：round-robin + 健康标记 + 重进轮换判定（纯逻辑，可测）
│   ├── gateway/
│   │   ├── anthropic.py   # POST /v1/messages 透传 + 故障转移循环
│   │   ├── openai.py      # POST /v1/chat/completions（OpenAI→Anthropic 翻译）(Phase 2)
│   │   ├── responses.py   # POST /v1/responses（Responses→Chat→Anthropic）(Phase 2)
│   │   ├── classify.py    # 上游错误 → 账号健康事件的分类器
│   │   └── headers.py     # 上游请求头构建（身份仿真）
│   ├── translator/        # OpenAI ↔ Anthropic 双向 + SSE 逐块翻译 (Phase 2，移植自 zcode-api 设计)
│   ├── quota.py           # 多端点额度探测 + PlanSlot 分组 + 错峰轮询（zcode-switch quota.rs 移植）
│   ├── claim.py           # billing/preview 轮询 + 自动领取 + 退避（claim.rs 移植）
│   ├── captcha.py         # 求解器编排：缓存 / 单飞 / 重试（zcode2api 保留）
│   ├── captcha_node/      # Node + jsdom 求解器 solver.js（zcode2api 原样移植）
│   ├── oauth.py           # zai server-mediated CLI 流 + bigmodel 授权码流（zcode2api + zcode-switch oauth.rs 补全）
│   ├── zclient.py         # enc:v1 编解码 + ~/.zcode/v2 三文件快照/切换 (Phase 3)
│   ├── bundle.py          # .zsb 兼容封包：PBKDF2(100k)+AES-256-GCM (Phase 1)
│   ├── auth_admin.py      # 后台 / 网关鉴权
│   └── routes/            # admin_api / pages / health
├── web/                   # 后台静态资源（accounts / quota / claim / settings 页）
├── tests/
│   ├── unit/              # pytest 单元（用例 ID 见测试文档 02）
│   ├── contract/          # 上游响应结构 fixture 契约测试
│   ├── interop/           # enc:v1 / .zsb 对拍向量
│   ├── mock_upstream/     # Mock ZCode 上游服务（集成测试核心资产）
│   └── e2e/               # compose 拉起全栈的端到端场景
├── main.py                # CLI：serve / login / accounts / quota / claim / export / import
├── config.example.yaml
├── Dockerfile / docker-compose.yml
└── docs/
```

### 模块间依赖规则

- `pool.py` 不依赖 httpx/fastapi —— 纯内存逻辑 + 时间注入，便于单元测试
- `gateway/*` 只通过 `pool.py` 取号、通过 `classify.py` 上报健康事件，不直接改账号状态
- `quota.py` / `claim.py` 复用 `captcha.py` 取验证码 token，不自己管理缓存
- `zclient.py` / `bundle.py` 是独立的加解密与文件操作层，不反向依赖 store

## 4. 核心流程

### 4.1 网关请求（含账号池故障转移）

```
client → 鉴权 → [循环: attempt ≤ pool.maxAttempts]
   1. PoolSelector 取号（round-robin，跳过 exhausted/cooling/invalid/expired）
   2. 构建上游请求（身份头 + 验证码头[start-plan] + 端点路由[coding-plan]）
   3. 发送（连接级失败重试 ×2）
   4. 响应分类：
      ok                → reportSuccess，透传/翻译返回
      402/额度关键词     → reportFailure(quota)，换号
      429               → reportFailure(rate_limited)，换号
      401/403(非验证码)  → reportFailure(auth_invalid)，换号
      403(验证码挑战)    → 刷新验证码原账号重试（≤3 次）
      其它              → 原样回传客户端
   5. 池内无可选号 → 503 no_available_account
```

### 4.2 账号健康状态机

```
            ┌─────────┐  402/额度关键词(30min 后自动重试)   ┌───────────┐
   login───▶│ ACTIVE  │◀──────────────────────────────────│ EXHAUSTED │
            │         │  429(冷却 300s)                    └───────────┘
            │         │◀──────┐        ┌─────────┐
            │         │       ├────────│ COOLING │
            │         │       └───────▶└─────────┘
            │         │  401/403 非验证码        ┌─────────┐
            │         │────────────────────────▶│ INVALID │ （直到重新登录）
            └─────────┘                         └─────────┘
   reportSuccess() 可清除 COOLING/EXHAUSTED；INVALID 仅重新登录（凭证 upsert）或重启清除
```

参数：`pool.max_attempts=4`、`pool.cooldown_seconds=300`、`pool.exhausted_retry_seconds=1800`（env 可覆盖）。

### 4.3 额度监控（错峰轮询）

后台任务按 `quota.refresh_interval`（默认 60s）分批探测池内账号；单轮内账号之间加随机抖动（stagger）避免固定节奏触发 WAF；每个账号并发探测多端点（`billing/balance` + `subscription/list` + `usage/quota/limit`），结果归并为 `QuotaOverview { PlanSlot[] }`。额度全部归零 → 标记 EXHAUSTED；检测到恢复 → 自动回 ACTIVE。

### 4.4 活动领取（ClaimScheduler）

```
每 claim.poll_interval(默认5min): GET billing/preview（Bearer JWT + X-Device-Mid）
  ├─ 404（活动未上线）→ 静默等待
  ├─ 有可领套餐 → 取验证码 verifyParam → POST billing/claim
  │    ├─ already_claimed / quota_exhausted → 按服务端返回的下次窗口退避
  │    └─ 其它失败 → cooldown(10min) 退避
  └─ 领取成功 → 写入 claim_history（SQLite），后台 UI 展示
支持 auto=false 的手动模式；领取作用于池内所有 ACTIVE 的 JWT 账号。
```

### 4.5 凭证进入池内的四条路径

| 路径 | 流程 | 阶段 |
|------|------|------|
| OAuth 登录（zai） | `POST /oauth/cli/init` → 浏览器授权 → 轮询换 token → 兑换 API Key + JWT | P1 |
| OAuth 登录（bigmodel） | bigmodel.cn 授权码 → 本地回调 → token 交换 | P1 |
| `.zsb` 包导入 | 口令解包 → 校验 → 入池（与 zcode-switch 互通） | P1 |
| 本机 ZCode 导入 | 读 `~/.zcode/v2/credentials.json` → enc:v1 解密出 `zcodejwttoken` → 入池 | P3 |

### 4.6 ZCode 客户端切换（可选，桌面场景）

快照 `~/.zcode/v2/{credentials,config,telemetry-state}.json` 三文件至账号库 → 原子换入目标账号（临时文件 + rename）→ 可选重启 ZCode 客户端。**切换前自动保全当前未入库登录，绝不丢号。**

## 5. 与来源项目的边界

| 能力 | 取自 | 不采用的部分 |
|------|------|--------------|
| 网关底座、captcha、admin 骨架 | zcode2api | 明文导出（换 .zsb）、简陋余额轮询（换 quota v2） |
| 额度模型、领取、enc:v1、.zsb、切换器 | zcode-switch | Tauri GUI / 托盘 / Win32 进程管理 / i18n 机制 |
| 池化故障转移设计、翻译层形态、签名/路由协议知识 | zcode-api（含本 fork 已实现的号池补丁） | TypeScript 运行时（翻译层用 Python 重写）、其无许可证代码（只借鉴设计与协议） |
