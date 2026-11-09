<!-- markdownlint-disable-file MD033 MD045 -->
<div align="center">

# ZCodeHub

**High-Availability Dual-Protocol Gateway (Anthropic & OpenAI) & Fleet Operations Engine**

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg?style=flat-square)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.11+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Architecture](https://img.shields.io/badge/Architecture-Dual--Protocol_%7C_Fingerprint_Pool-orange.svg?style=flat-square)](#core-capabilities)
[![Tests](https://img.shields.io/badge/tests-813%20passed-brightgreen.svg?style=flat-square)]()

[English](./README.md) · [简体中文](./README.zh-CN.md) · [Core Capabilities](#core-capabilities) · [Architecture](#system-architecture) · [Quick Start](#quick-start) · [API Specs](#api-protocol-specifications) · [Account Lifecycle](#account-state-machine--failover)

<br>

<p align="center">
  <b>Terminates credential rotation chaos, protocol fragmentation, and automated downtime caused by upstream anti-bot triggers.</b><br>
  Engineered natively on Python 3.11+ and FastAPI async runtimes with emulated desktop SKU hardware fingerprints and headless captcha solving.<br>
  Transforms ZCode Coding Plan and API Key quotas into standardized Anthropic Messages and OpenAI Chat Completions protocols with sticky session affinity, bounded 2-slot concurrency per account, laddered failover, and complete audit telemetry.
</p>

</div>

---

## Motivation & Architecture Comparison

When integrating Claude Code, Cline, Codex CLI, or custom coding agents with upstream AI services, engineers frequently encounter manual token rotation overhead, non-standard protocol dialects, sliding verification obstacles, and cascading service outages when single accounts hit rate limits:

| Evaluation Dimension | ZCodeHub Operations Engine | Ad-Hoc Reverse Proxy Scripts | Generic API Hubs |
| :--- | :--- | :--- | :--- |
| **Protocol Support** | Native dual-protocol gateway (Anthropic SSE streaming + bidirectional OpenAI translation) | Single protocol passthrough; cannot bridge Claude and OpenAI ecosystems | Rigid protocol mapping prone to dropping system prompts and streaming events |
| **Hardware Fingerprints**| Independent per-account SKU profiles (Mac Sequoia/Win11) with launch telemetry | Exposes host Linux kernel and reverse-proxy headers, triggering cluster bans | Lacks client emulation, causing correlated account lockouts |
| **Captcha Solving** | Headless Node.js + jsdom sandbox solving Aliyun slide verifications in seconds | Incapable of passing verification challenges; limited to low-quota keys | Relies on expensive, high-latency third-party solving APIs |
| **Concurrency & Affinity**| Hard limit of 2 concurrent slots per account, session affinity for Prompt Cache reuse | No concurrency control; hung requests stall the entire queue | Basic round-robin across keys without context persistence |
| **Failover & Automation**| 429 adaptive backoff, 3012 risk-control circuit breaking, and automated package claims | Fails immediately on 429; no intelligent backoff or channel rotation | No automated synchronization with upstream promotional benefits |

---

## Core Capabilities

| Module | Feature Highlight | Technical Implementation & Advantage |
| :--- | :--- | :--- |
| **Dual-Protocol Gateway** | Bidirectional translation | Native `POST /v1/messages` (Anthropic SSE streaming) and `POST /v1/chat/completions` (OpenAI format translation); includes `GET /v1/models`. |
| **Trusted Fingerprint Pool**| Strict 1:1 hardware isolation| Generates weighted desktop SKU hardware profiles (Mac Sequoia/Tahoe, Win11) with distinct UUIDv4 `device_mid` per account to eliminate correlation. |
| **Launch Sequence Telemetry**| Full lifecycle emulation | Emulates official client onboarding; executes `client/configs` and activation event logging (`app_launch` / `app_daily_active`) with persistent timestamps. |
| **Header Sanitization** | Topology leakage guard | Strips `x-forwarded-*`, `cf-*`, `cdn-loop`, `x-real-ip`, `via`, and proxy metadata to avoid leaking backend deployment topology. |
| **Session Affinity Routing**| Context cache maximization | Extracts session headers or message hashes for sticky routing to maximize Prompt Cache hits; hard-limits concurrency to 2, overflowing gracefully when full. |
| **Laddered Failover Engine** | Adaptive backoff & breaker | Respects `Retry-After` on 429 while releasing active slots; falls back to API Key upon exhaustion; puts 5xx on 300s cooldown; disables accounts on 3012/405. |
| **Headless Captcha Solver** | Zero-Chrome Node/jsdom sandbox | Executes official Aliyun SDK inside isolated Node processes; includes warm-up pools, 45s caching, and concurrency deduplication without heavy Chrome instances. |
| **Automated Package Claims**| Promotional quota tracking | Automatically claims promotional benefit packages on onboarding and runs a 600s periodic inspection; safely pauses when hitting upstream `next_at` windows. |

---

## System Architecture

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        AI Clients & Coding Agents                      │
│   Claude Code · Cline · Codex CLI · Chatbox · NextChat · Custom Tools  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTPS (Anthropic Messages / OpenAI Chat)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   ZCodeHub Gateway & Operations Hub (:3000)            │
│  ┌──────────────────────┐ ┌──────────────────────┐ ┌────────────────┐  │
│  │ Protocol Translation │ │ Affinity & Concurrency │ │ Failover Engine│  │
│  │ /v1/messages         │ │ Session Sticky Routing │ │ 429 Backoff/Key│  │
│  │ /v1/chat/completions │ │ Concurrency Capped @ 2 │ │ 3012 Breaker   │  │
│  └──────────┬───────────┘ └──────────┬───────────┘ └────────┬───────┘  │
│             │                        │                      │          │
│             ▼                        ▼                      ▼          │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │    Account Fleet Manager (SQLite WAL · Hardware Pool · Quota)    │  │
│  └──────────────────────────────────┬───────────────────────────────┘  │
└─────────────────────────────────────┼──────────────────────────────────┘
                                      │ Upstream Requests with Emulated Headers
        ┌─────────────────────────────┴─────────────────────────────┐
        ▼                                                           ▼
┌────────────────────────────────┐          ┌────────────────────────────────┐
│   Node.js Captcha Sandbox      │          │   Upstream LLM Infrastructure  │
│   (captcha_node / jsdom)       │          │   ├─ Z.AI Plan Channel (JWT)   │
│   Headless Aliyun Captcha SDK  │          │   ├─ Z.AI API Key Fallback     │
│   45s Cache · Zero Chrome Deps │          │   └─ BigModel Open Platform    │
└────────────────────────────────┘          └────────────────────────────────┘
```

---

## Quick Start

### 1. Prerequisites & Environment Setup

Requirements: **Python 3.11+** and **Node.js 20+** (required for the captcha solver sandbox):

```bash
# Clone the repository
git clone https://github.com/dengyie/zcode2api.git
cd zcode2api

# Create and activate Python virtual environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Install dependencies for the headless captcha sandbox
cd captcha_node && npm install && cd ..
```

### 2. Configure & Run Service

```bash
# Copy configuration template
cp .env.example .env

# Adjust ZCODE_ADMIN_KEY and network settings in .env as needed
# Start the gateway engine and web console (defaults to http://0.0.0.0:3000)
.venv/bin/python cli.py serve
```

Once running:
- **Management Console**: `http://127.0.0.1:3000/admin/login` (Admin key defined in `.env`)
- **Anthropic Protocol Endpoint**: `POST http://127.0.0.1:3000/v1/messages`
- **OpenAI Protocol Endpoint**: `POST http://127.0.0.1:3000/v1/chat/completions`
- **Model Registry Endpoint**: `GET http://127.0.0.1:3000/v1/models`
- **Health Check Probe**: `GET http://127.0.0.1:3000/meta`

### 3. Account Ingestion

```bash
# Option 1: Headless OAuth Login (Automatic ingestion and fallback key exchange)
.venv/bin/python cli.py login zai

# Option 2: Add credentials manually via CLI (JWT or API Key)
.venv/bin/python cli.py add-account zai primary-account <JWT_OR_KEY>
```

---

## API Protocol Specifications

### Anthropic Messages Endpoint (`POST /v1/messages`)

Fully compatible with Claude Code and Cline, supporting SSE streaming:

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

### OpenAI Chat Endpoint (`POST /v1/chat/completions`)

Translates chat requests, streaming chunks, and tool invocations:

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

## Account State Machine & Failover

The gateway enforces atomic transitions across account states to ensure uninterrupted routing:

```text
            ┌─────────┐   Quota Refreshed           ┌──────────┐
  Login ───>│ ACTIVE  │<────────────────────────────│ EXHAUSTED│
            │         │   5xx Cooldown Settles      └──────────┘
            │         │<───────┐      ┌─────────┐
            │         │        ├──────│ COOLING │
            │         │        └─────>└─────────┘
            │         │ 401/403 (Non-Captcha Auth Fail)
            │         │─────────────────────────────> INVALID (Re-authentication required)
            │         │ 3012/405 (Anti-Bot Triggers)
            │         │─────────────────────────────> DISABLED (Protected, manual recovery)
            └─────────┘
```

- **Concurrency Capped at 2**: When an account reaches its 2 active slots, incoming requests overflow immediately to the next available account without waiting or inflating retry counts.
- **Adaptive 429 Handling**: Waits in-place based on upstream `Retry-After` headers while releasing its concurrency slot; switches to fallback API Key channels if 429 persists.
- **Circuit Breaker on 3012/405**: Immediately marks flagged accounts as `DISABLED` to protect assets from further automated probing until verified via admin console.

---

## Configuration Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `ZCODE_PORT` | `3000` | Port for the gateway server and management console |
| `ZCODE_HOST` | `0.0.0.0` | Server bind host address |
| `ZCODE_ADMIN_KEY` | `change-me` | Initial console password (persisted to SQLite on startup) |
| `ZCODE_GATEWAY_KEY` | None | Gateway client authentication key (leave empty to disable check) |
| `ZCODE_DATA_DIR` | `data` | Storage directory for SQLite database and state registries |
| `ZCODE_QUOTA_REFRESH_INTERVAL` | `60` | Background quota refresh frequency in seconds (0 to disable) |
| `ZCODE_COOLING_SECONDS` | `300` | Cooldown duration in seconds after 5xx upstream failures |
| `ZCODE_CLAIM_ROUND_INTERVAL` | `600` | Interval in seconds between automated benefit package claim checks |
| `ZCODE_NODE_PATH` | `node` | Executable path for Node.js runtime |
| `ZCODE_CAPTCHA_RETRIES` | `4` | Maximum retries allowed for captcha verification solving |
| `CAPTCHA_CACHE_TTL` | `45000` | Lifetime of cached captcha solutions in milliseconds |

---

## Testing & Quality Assurance

The repository includes offline regression suites with simulated upstream endpoints and fault injection:

```bash
# Run complete test suite (813 tests passing offline)
.venv/bin/python -m pytest tests/

# Execute static linting and type boundary assertions
.venv/bin/ruff check app tests
.venv/bin/mypy app/constants.py
```

---

## Contributing & License

Contributions are welcome. Please ensure all tests pass prior to submitting changes:

```bash
.venv/bin/python -m pytest tests/
```

- Licensed under the [AGPL-3.0 License](LICENSE).
- Disclaimer: This project is intended strictly for technical research, personal education, and internal testing. Please comply with all upstream terms of service and relevant local regulations.
