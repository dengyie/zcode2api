<!-- markdownlint-disable-file MD033 MD045 -->
<div align="center">

# ZCodeHub

**高可用双协议网关 (Anthropic & OpenAI) 与 ZCode 账号运营调度中枢**

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg?style=flat-square)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.11+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Architecture](https://img.shields.io/badge/Architecture-Dual--Protocol_%7C_Fingerprint_Pool-orange.svg?style=flat-square)](#核心能力)
[![Tests](https://img.shields.io/badge/tests-813%20passed-brightgreen.svg?style=flat-square)]()

[English](./README.md) · [简体中文](./README.zh-CN.md) · [核心能力](#核心能力) · [系统架构](#系统架构全景) · [快速上手](#快速上手) · [API 规范](#api-协议接口规范) · [状态机与容灾](#多账号轮询与状态机)

<br>

<p align="center">
  <b>彻底告别多账号轮换混乱、客户端协议割裂以及云端风控导致的自动化停转。</b><br>
  严格基于 Python 3.11+ 与 FastAPI 高并发异步运行时构建，提供成套高可信桌面指纹仿真与免浏览器的无痕验证求解。<br>
  将 ZCode Coding Plan 与 API Key 额度无缝转换为标准的 Anthropic Messages 与 OpenAI Chat Completions 协议，支持会话亲和性路由、单号并发硬隔离、阶梯式故障转移与全景请求审计。
</p>

</div>

---

## 痛点与架构对比

在为 Claude Code、Cline、Codex CLI 或各类 AI 编码客户端配置上游模型服务时，个人与团队常面临多账号手工切换繁琐、上游协议非标、云厂商无痕验证码拦截以及因单号并发打满导致整体雪崩的困境：

| 评估维度 | ZCodeHub 运营调度中枢 | 简易反代脚本 | 传统 API 聚合网关 |
| :--- | :--- | :--- | :--- |
| **协议兼容能力** | 原生双协议支持（Anthropic SSE 流式 + OpenAI 双向实时翻译），无缝接入各客户端 | 仅支持单一协议转发，难以同时兼容 Claude 与 OpenAI 生态 | 协议转换规则僵硬，易丢失上下文系统提示词与流式事件 |
| **设备指纹可信度**| 一号一台独立成套 SKU（Mac Sequoia/Win11），带首启安装序上报与网络头深度清洗 | 暴露宿主机真实 Linux 内核与网络代理中间件头，极易被上游风控连坐封号 | 缺乏客户端特征伪装，多账号共享请求上下文导致关联封禁 |
| **无痕验证求解** | 独立 Node.js + jsdom 沙箱，免真实浏览器秒级求解阿里云滑块与无痕参数 | 无法突破上游验证码，仅能使用免验证码的低额度回退通道 | 依赖外部昂贵的打码平台服务，时延不可控且成本高昂 |
| **并发治理与亲和**| 严格单号并发恒为 2，支持会话上下文亲和性路由（Prompt Cache 复用）与满载无缝溢出 | 无并发控制或盲目排队，单请求卡死导致整体请求队列拥塞 | 仅支持粗粒度按 Key 轮询，无法粘性绑定多轮对话上下文 |
| **容灾与限时套餐**| 429 智能退避重试切 Key，3012 真风控熔断保护，后台周期全自动领取限时活动套餐 | 遭遇 429 直接报错，无账号退避与自动换号机制 | 缺乏与上游活动联动的生命周期管理，错过活动福利投放 |

---

## 核心能力

| 模块 | 特性亮点 | 技术实现与工程优势 |
| :--- | :--- | :--- |
| **双协议兼容网关** | 原生双向协议适配 | 原生支持 `POST /v1/messages`（Anthropic SSE 流式）与 `POST /v1/chat/completions`（OpenAI 格式双向翻译），提供 `GET /v1/models` 端点。 |
| **高可信桌面指纹池**| 一号一台物理隔离 | 入池自动生成加权桌面成套 SKU（Mac Sequoia/Tahoe、Win11 主流分辨率），每账号独立全新 UUIDv4 `device_mid`，杜绝关联。 |
| **官方首启安装序** | 完整行为生命周期 | 入池与指纹轮换自动执行 `client/configs` 与激活事件上报（`app_launch` / `app_daily_active`），持久化 `install_id` 与 `installed_at`。 |
| **网络基础设施清洗**| 消除反代拓扑泄露 | 自动剔除 `x-forwarded-*`、`cf-*`、`cdn-loop`、`x-real-ip`、`via` 等网络中间件头部，杜绝反代指纹泄漏。 |
| **会话亲和性与防打满**| 上下文缓存与并发上限 | 提取显式 Header 或消息指纹实现会话粘性路由以复用 Prompt Cache；单账号写死并发上限为 2，满载时无损溢出至其他账号。 |
| **阶梯故障转移策略**| 智能退避与风控熔断 | 429 按 `Retry-After` 原地释放并发等待，耗尽切 API Key；5xx 重试 3 次冷却 300s；3012/405 识别后直接标记 `DISABLED` 防止损毁。 |
| **免浏览器无痕求解**| Node + jsdom 沙箱 | 独立子进程运行阿里云无痕 SDK，内置预热池、45s 缓存与并发去重，无需安装体积庞大的无头 Chrome。 |
| **限时套餐自动领取**| 活动福利精准追踪 | 入池自动领取，后台每 600 秒周期巡检；即便被停用、风控禁用的账号照常领包；遵循上游 1005 名额耗尽 `next_at` 智能避让。 |

---

## 系统架构全景

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        AI 客户端与编码 Agent 矩阵                      │
│   Claude Code · Cline · Codex CLI · Chatbox · NextChat · 自定义工作流  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTPS (Anthropic Messages / OpenAI Chat)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   ZCodeHub 高可用网关与调度中枢 (:3000)                │
│  ┌──────────────────────┐ ┌──────────────────────┐ ┌────────────────┐  │
│  │ 协议翻译引擎         │ │ 亲和性调度与并发门禁 │ │ 阶梯式容灾引擎 │  │
│  │ /v1/messages         │ │ Session Affinity     │ │ 429 退避/换号  │  │
│  │ /v1/chat/completions │ │ 单号并发恒为 2 (溢出)│ │ 3012 风控熔断  │  │
│  └──────────┬───────────┘ └──────────┬───────────┘ └────────┬───────┘  │
│             │                        │                      │          │
│             ▼                        ▼                      ▼          │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │     账号池管理中心 (SQLite WAL · 桌面指纹池 · 自动套餐巡检)      │  │
│  └──────────────────────────────────┬───────────────────────────────┘  │
└─────────────────────────────────────┼──────────────────────────────────┘
                                      │ 携带验证参数与桌面指纹
        ┌─────────────────────────────┴─────────────────────────────┐
        ▼                                                           ▼
┌────────────────────────────────┐          ┌────────────────────────────────┐
│   Node.js 验证码求解沙箱       │          │   上游大模型服务集群           │
│   (captcha_node / jsdom)       │          │   ├─ Z.AI Plan 通道 (JWT)      │
│   免浏览器运行阿里无痕 SDK     │          │   ├─ Z.AI API Key 回退通道     │
│   45s 预热缓存 · 零 Chrome 依赖│          │   └─ 智谱开放平台 (BigModel)   │
└────────────────────────────────┘          └────────────────────────────────┘
```

---

## 快速上手

### 1. 环境准备与依赖安装

系统要求：**Python 3.11+** 以及 **Node.js 20+**（用于验证码求解沙箱）：

```bash
# 克隆代码仓库
git clone https://github.com/dengyie/zcode2api.git
cd zcode2api

# 创建并激活 Python 虚拟环境
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 初始化无痕验证码 Node.js 沙箱依赖
cd captcha_node && npm install && cd ..
```

### 2. 配置与启动服务

```bash
# 复制配置文件模板
cp .env.example .env

# 按需修改 .env 中的 ZCODE_ADMIN_KEY 与端口配置
# 启动调度服务与管理后台（默认监听 http://0.0.0.0:3000）
.venv/bin/python cli.py serve
```

服务就绪后：
- **运营后台**：`http://127.0.0.1:3000/admin/login`（默认密码查看 `.env` 中 `ZCODE_ADMIN_KEY`）
- **Anthropic 协议端点**：`POST http://127.0.0.1:3000/v1/messages`
- **OpenAI 协议端点**：`POST http://127.0.0.1:3000/v1/chat/completions`
- **模型列表接口**：`GET http://127.0.0.1:3000/v1/models`
- **探活接口**：`GET http://127.0.0.1:3000/meta`

### 3. 添加账号入池

```bash
# 方式一：OAuth 登录（免密，自动入池并兑换回退 Key）
.venv/bin/python cli.py login zai

# 方式二：命令行手动添加凭据（JWT 或 API Key）
.venv/bin/python cli.py add-account zai 主号名 <JWT_OR_KEY>
```

---

## API 协议接口规范

### Anthropic Messages 协议 (`POST /v1/messages`)

原生兼容 Claude Code 与 Cline，支持 SSE 流式返回：

```bash
curl -X POST http://127.0.0.1:3000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: your-gateway-key" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### OpenAI Chat 协议 (`POST /v1/chat/completions`)

双向自动翻译工具调用与流式增量：

```bash
curl -X POST http://127.0.0.1:3000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-gateway-key" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

---

## 多账号轮询与状态机

网关为池中所有账号维护原子状态流转，杜绝因单个账号故障阻断整体链路：

```text
            ┌─────────┐   额度用完(定期自动再探)    ┌──────────┐
  登录 ────>│ ACTIVE  │<─────────────────────────│ EXHAUSTED│
            │         │   5xx 重试耗尽(冷却 N 秒) └──────────┘
            │         │<───────┐      ┌─────────┐
            │         │        ├──────│ COOLING │
            │         │        └─────>└─────────┘
            │         │ 401/403 (非验证码)
            │         │──────────────────────> INVALID（凭证失效，需重新授权）
            │         │ 3012/405 (真风控)
            │         │──────────────────────> DISABLED（风控保护，后台手动恢复）
            └─────────┘
```

- **单账号并发硬隔离**：所选账号在飞并发达上限（2）时直接跳过溢出，不阻塞也不计入重试错误。
- **429 智能重试**：优先按上游 `Retry-After` 原地等待重试（期间释放并发槽位）；耗尽后无损换号且**不冷却账号**。
- **3012/405 风控熔断**：捕获 "unusual activity" 立即标记 `DISABLED` 停用账号保护资产，支持后台确认后一键恢复。

---

## 环境变量参考

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `ZCODE_PORT` | `3000` | 网关与管理面板监听端口 |
| `ZCODE_HOST` | `0.0.0.0` | 服务绑定地址 |
| `ZCODE_ADMIN_KEY` | `change-me` | 后台管理密码初始值（首次启动写入数据库） |
| `ZCODE_GATEWAY_KEY` | 空 | 网关 API 鉴权密钥（留空则不校验，生产环境务必设置） |
| `ZCODE_DATA_DIR` | `data` | SQLite 数据库与持久化数据目录 |
| `ZCODE_QUOTA_REFRESH_INTERVAL` | `60` | 后台刷新账号额度频率（秒，0 表示关闭） |
| `ZCODE_COOLING_SECONDS` | `300` | 5xx 限流冷却时长（秒） |
| `ZCODE_CLAIM_ROUND_INTERVAL` | `600` | 活动套餐自动轮询领取周期（秒） |
| `ZCODE_NODE_PATH` | `node` | 求解验证码所用的 Node.js 可执行文件路径 |
| `ZCODE_CAPTCHA_RETRIES` | `4` | 单次验证码求解重试次数上限 |
| `CAPTCHA_CACHE_TTL` | `45000` | 验证码计算结果复用缓存时长（毫秒） |

---

## 自动化测试套件

本项目内置覆盖全量端点模拟与故障注入的离线 Mock 回归测试，无需依赖真实网络：

```bash
# 执行全量 813 项单元与端到端测试
.venv/bin/python -m pytest tests/

# 执行代码风格与类型合规检查
.venv/bin/ruff check app tests
.venv/bin/mypy app/constants.py
```

---

## 参与贡献与许可证

欢迎提交 Issue 与 Pull Request！在提交代码变更前，请确保全量测试套件执行通过：

```bash
.venv/bin/python -m pytest tests/
```

- 本项目采用 [AGPL-3.0](LICENSE) 开源协议。
- 免责声明：本仓库仅供技术研究、个人学习与内部验证使用，请在遵守服务条款与相关法律法规的前提下使用。
