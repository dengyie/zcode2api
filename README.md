# zcode2api

**ZCode 账号运营 + 网关一体机**：把池内 ZCode Coding Plan / Start Plan 账号额度，统一转换为标准
**Anthropic Messages API**（`/v1/messages`）暴露给任意 Agent（Claude Code、Cline 等）。

自带一套完整的账号运营控制台：多账号池轮询、额度用完自动换号、实时用量监控、免登录限时套餐自动领取、
OAuth 免密登录入池，以及免浏览器的阿里云无痕验证求解——开箱即用，自托管部署。

---

## 核心能力

- **标准网关**：`POST /v1/messages`（兼容 Anthropic Messages 协议）+ `GET /v1/models`，流式透传。
- **多账号轮询与故障转移**：请求按 round-robin 分发到账号池；额度用完 / 限流 / 鉴权失效自动换下一个可用账号。
- **实时用量监控**：后台周期刷新各账号额度，UI 实时展示每个账号的状态、模型剩余额度、调用与失败次数；额度耗尽自动标记，恢复自动复活。
- **套餐自动领取**：监听可领取限时套餐（`billing/preview` + `billing/claim`），对池内全部 JWT 账号自动领取，遇验证码失败自动换码重试。
- **OAuth 免密登录**：浏览器授权 → 自动保存 Coding Plan JWT 入池，并自动兑换 API Key 作为同账号回退通道。
- **免浏览器无痕验证**：用 **Node + jsdom** 在模拟浏览器环境中运行阿里云无痕 SDK，求解 `verifyParam`，无需真实浏览器 / 无头 Chromium。
- **后台管理 UI**：登录、账号池增删改启禁、一键导入导出、网关 / 后台密钥与监控参数设置。

## 支持的账号类型

| Provider | 模式 | 说明 |
|----------|------|------|
| `zai` | `jwt` | Coding Plan 额度（Plan 通道，需无痕验证码），自动续领 |
| `zai` | `apiKey` | Z.AI API Key（回退通道，免验证码） |
| `bigmodel` | `apiKey` | 智谱开放平台（Anthropic 兼容端点） |

> 一个 Z.AI 账号可同时持有 JWT 与 API Key：OAuth 登录后会一并入池，JWT 额度告罄或需要验证码时自动走 API Key 回退。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # 按需修改密钥、端口等
.venv/bin/python cli.py serve   # 启动网关 + 后台 UI（默认 http://0.0.0.0:3000）
```

- 后台管理：`http://127.0.0.1:3000/admin/login`（初始账号见 `.env` 的 `ZCODE_ADMIN_KEY`）
- 对话端点：`POST http://127.0.0.1:3000/v1/messages`（Anthropic Messages 协议，兼容 Claude Code）

> 使用 Z.AI **JWT 模式**需要 Node.js 求解验证码，首次先执行：`cd captcha_node && npm install`。

## 快速上手一个账号

```bash
# 方式一：OAuth 登录（免密，自动入池 + 自动兑换回退 Key）
.venv/bin/python cli.py login zai

# 方式二：手动加入现成凭证（JWT 三段点分 或 API Key）
.venv/bin/python cli.py add-account zai 名称 <jwt|key>
```

## 命令行

```
python cli.py serve [--port 3000]    启动网关 + 后台 UI
python cli.py login zai              通过 OAuth 登录 Z.AI 并自动入池
python cli.py add-account <zai|bigmodel> <name> <jwt|key>   添加账号
python cli.py accounts [zai|bigmodel]  查看账号列表
python cli.py remove-account <provider> <id|name>  删除账号
python cli.py quota                  查看各账号实时额度
python cli.py status                 查看配置概览
python cli.py set-admin-key <key>    设置后台密码
python cli.py export [file]          导出全部账号
python cli.py import <file>          导入账号
```

## 后台 UI

| 页面 | 说明 |
|------|------|
| `/admin/login` | 后台登录（Bearer 密钥鉴权，凭证加密存于浏览器 localStorage）|
| `/admin/accounts` | 账号池：新增/导入/导出、启用禁用、**实时额度与状态监控**、套餐领取 |
| `/admin/settings` | 后台密码、网关 API Key、额度刷新间隔 |

账号池页实时展示每个账号的**状态（正常 / 额度用完 / 限流 / 异常 / 禁用）**、各模型剩余额度、
调用与失败次数；并提供「手动刷新额度」与「批量领取套餐」入口。

## 多账号轮询与故障转移

- 在账号池粘贴 Coding Plan JWT（3 段点分）或 API Key，每行一个即可加入轮询。
- 网关每次请求选择下一个「可用」账号，跳过用完 / 限流 / 异常 / 禁用的账号。
- **额度用完**（余额为 0 / 上游 402 / 错误体含 quota、余额不足等关键词）→ 标记 `exhausted`，换下一个账号。
- **上游 429** → 标记 `cooling` 冷却一段时间后自动恢复。
- **验收 401/403（非验证码）** → 标记 `invalid`，需重新登录。
- **验证码过期（403 / code 3007）** → 原地刷新验证码对同一账号重试（最多 3 次）。
- 达到请求上限仍无可用账号 → 返回 `503 no_available_account`。

账号状态机：

```
            ┌─────────┐   额度用完(定期自动再探)    ┌──────────┐
  登录 ────▶│ ACTIVE  │◀─────────────────────────│ EXHAUSTED│
            │         │   429(冷却 N 秒)          └──────────┘
            │         │◀───────┐      ┌─────────┐
            │         │        ├──────│ COOLING │
            │         │        └─────▶└─────────┘
            │         │ 401/403(非验证码)
            │         │──────────────────────▶ INVALID（还原后重新登录）
            └─────────┘
```

> 成功响应可清除 `COOLING` / `EXHAUSTED`；额度恢复时后台监控也会自动回 `ACTIVE`。

## 无痕验证（免浏览器）

JWT 账号调用上游时需携带阿里云无痕验证参数（请求头 `X-Aliyun-Captcha-Verify-Param`）。
本项目**不启动真实浏览器**，而是用 **Node + jsdom** 在模拟浏览器环境中运行阿里云官方无痕 SDK 直接求解该参数。

- 求解器位于 `captcha_node/solver.js`，首次使用前执行 `cd captcha_node && npm install`。
- `app/captcha.py` 以子进程方式调用，内置结果缓存（默认 45s）、并发去重（同一时刻只跑一个求解进程）与失败重试。
- 求解器在 jsdom 中补齐了 SDK 依赖的浏览器 API（`matchMedia`、canvas/WebGL、`Worker`、`OffscreenCanvas`）。
- 配置与会话缓存兜底：`client/configs` 拉取失败时回落内置默认参数。

## 鉴权

- **后台鉴权**：所有 `/admin/api/*` 需 `Authorization: Bearer <后台密码>`（也支持 `?app_key=`）。
- **网关鉴权（可选）**：在「设置」配置「网关 API Key」后，`/v1/messages` 须携带
  `Authorization: Bearer <key>` 或 `x-api-key: <key>`；留空则不校验。

## 环境变量

`cp .env.example .env` 后按需修改（.env 永不入库）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ZCODE_PORT` | 3000 | 服务端口 |
| `ZCODE_HOST` | 0.0.0.0 | 监听地址 |
| `ZCODE_ADMIN_KEY` | change-me | 后台密码初始值（之后写库，以库为准）|
| `ZCODE_GATEWAY_KEY` | 空 | 网关 API Key；留空不校验（生产务必设置）|
| `ZCODE_DATA_DIR` | data | 数据目录（SQLite 存放处）|
| `ZCODE_QUOTA_REFRESH_INTERVAL` | 60 | 后台刷新额度间隔（秒），0 关闭 |
| `ZCODE_COOLING_SECONDS` | 300 | 限流冷却时长（秒）|
| `ZCODE_NODE_PATH` | node | 验证码求解所用 Node 可执行文件 |
| `ZCODE_CAPTCHA_RETRIES` | 4 | 单次求解失败重试次数 |
| `ZCODE_CAPTCHA_TIMEOUT` | 40 | 单次求解超时（秒）|
| `CAPTCHA_CACHE_TTL` | 45000 | 验证码结果缓存时长（ms）|
| `ZAI_UPSTREAM_URL` / `ZAI_FALLBACK_URL` / `BIGMODEL_UPSTREAM_URL` | — | 上游端点覆盖 |

## 开发与测试

```bash
.venv/bin/python -m pytest            # 全量测试（Mock 上游，无真实网络）
.venv/bin/ruff check app tests
.venv/bin/mypy app/constants.py
```

测试全部走 **Mock 上游**：`tests/mock_upstream/` 模拟 Z.AI 被依赖的全部端点，
支持 `x-mock-scenario` 故障注入（具体见 `docs/testing`），不依赖真实网络，可离线回归。

## 文档

完整文档见 [`docs/README.md`](docs/README.md)：

- **开发**：架构 · 数据格式 · API 规范 · 上游协议 · 开发指南 · 路线图
- **测试**：测试策略 · 单元用例 · 互通向量 · 集成 E2E · 验收标准

## 技术栈

- Python 3.12+ · FastAPI · Uvicorn · httpx
- SQLite（账号 / 设置持久化，WAL 模式）
- Node.js + jsdom（免浏览器求解阿里云无痕验证）

## 许可证

本项目采用 [AGPL-3.0](LICENSE) 许可证。

## 免责声明

本仓库仅供学习、研究、个人实验与内部验证使用，不提供任何形式的商业授权、适用性保证或结果保证。

作者不因使用、修改、分发、部署或依赖本项目产生的任何直接或间接损失、账号封禁、数据丢失、
法律风险或第三方索赔负责。请勿将本项目用于违反服务条款、协议、法律或平台规则的场景；商业前请自行确认许可证与相关协议。