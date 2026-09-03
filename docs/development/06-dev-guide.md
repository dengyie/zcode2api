# 06 — 开发指南

## 1. 环境搭建

```bash
# 依赖：Python 3.12+、Node 20+（验证码求解器）、（可选）docker
git clone <zcode-hub 仓库> && cd zcode-hub
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # fastapi uvicorn httpx cryptography pytest pytest-asyncio respx
cd captcha_node && npm install && cd ..  # jsdom（验证码求解器依赖）
cp config.example.yaml config.yaml       # 按需修改
```

本地跑起来：

```bash
python main.py serve                     # 网关 + 后台，默认 http://127.0.0.1:3000
python main.py login zai                 # OAuth 登录（浏览器授权，凭证入池）
python main.py accounts / quota / status # 巡检
```

## 2. 测试命令

```bash
pytest tests/unit -q                     # 单元（无网络）
pytest tests/interop -q                  # enc:v1 / .zsb 对拍向量
pytest tests/contract -q                 # 上游响应结构契约
docker compose -f tests/mock_upstream/compose.yaml up -d   # Mock 上游 :9901
ZCODE_MOCK_UPSTREAM=http://127.0.0.1:9901 pytest tests/integration -q
docker compose -f tests/e2e/compose.yaml up --build --abort-on-container-exit  # 全栈 E2E
pytest --cov=app --cov-report=term-missing  # 覆盖率（门禁见测试文档 01）
```

## 3. 编码规范

- **类型注解全覆盖**（`mypy --strict` 为目标，至少 `pyright basic` 过）；dataclass 优先。
- 模块依赖方向遵守 01 文档 §3 的规则；`pool.py` / `classify.py` / `translator/` 必须保持无 IO（时间用 `time.time` 注入参数）。
- 上游常量（URL、模型名、关键词表、头名单）一律收口到 `app/constants.py`，禁止散落字面量——它们是风控联动的敏感点。
- 错误分类、状态机转移**必须有对应测试 ID**（见测试文档 02），改行为先改用例。
- 日志：请求行沿用 `#id | fmt | model | status | ttfb | tokens` 表格风格；池换号必须打 `#id account failed (<reason>)` 便于 grep。
- 提交信息 `feat|fix|test|docs|refactor(scope): ...`；每个 Phase 对应里程碑分支 `phase/N`。

## 4. Mock 上游（开发期默认挂接）

`tests/mock_upstream/` 是 FastAPI 应用，模拟 zcode.z.ai / api.z.ai / open.bigmodel.cn 全部端点（见测试文档 04 的注入矩阵）。开发时通过环境变量把上游指过去：

```bash
ZAI_UPSTREAM_URL=http://127.0.0.1:9901/api/v1/zcode-plan/anthropic/v1/messages \
ZAI_FALLBACK_URL=http://127.0.0.1:9901/api/anthropic/v1/messages \
ZCODE_MOCK_UPSTREAM=http://127.0.0.1:9901 python main.py serve
```

Mock 的故障注入用请求头控制（`x-mock-scenario: quota_exhausted | rate_limited | auth_invalid | captcha_challenge | captcha_3007 | sse_ok | sse_truncate | slow_first_byte`）。

## 5. 构建与部署

### Docker（通用）

```bash
docker compose up -d --build       # 含 Python + Node 双运行时；数据卷 /data
```

### tebi 容器约定（现网部署目标）

tebi 是无 systemd 的 LXC 容器，持久卷在 `/personal`（阿里云 NAS），进程管理用 supervisor：

```
/personal/zcode-hub/                 # 代码 + venv + data/
/etc/supervisor/conf.d/zcode-hub.conf
[program:zcode-hub]
directory=/personal/zcode-hub
command=/personal/zcode-hub/.venv/bin/python main.py serve /personal/zcode-hub/config.yaml
environment=TZ="Asia/Shanghai",ZCODE_DATA_DIR="/personal/zcode-hub/data",ZCODE_MASTER_SECRET="..."
autorestart=true
stdout_logfile=/personal/zcode-hub/logs/out.log
```

常用操作：`supervisorctl reread && supervisorctl update && supervisorctl status zcode-hub`。
发布流程：本地 `pytest` 全绿 → 构建/拉取镜像或 rsync 代码 → `supervisorctl restart zcode-hub` → 冒烟（`/health` + 一次真实 `/v1/messages`）。

## 6. 目录与命名

- 模块名单数（`store.py`/`pool.py`）；测试文件与被测模块同名 `test_<module>.py`。
- 上游模型名常量保留官方大小写（`GLM-5.2`），映射表 key 用小写。
- 时间字段统一 unix 秒（float），UI 层负责本地化。

## 7. 常见排障

| 症状 | 排查 |
|------|------|
| 全部请求 503 no_available_account | `GET /admin/api/pool` 看状态分布；`billing` 端点 401 多为 JWT 过期（需重登）而非无额度 |
| 验证码连续失败 | 确认 `captcha_node/node_modules` 已装；`ZCODE_CAPTCHA_TIMEOUT` 调大；阿里云指纹逻辑变更时需更新 solver.js 的浏览器 API 桩 |
| 额度一直是 0 / 401 | WAF 拦截：检查是否带全套身仿真头；错峰参数是否被调成 0 |
| 领取一直 ineligible | `identity.appVersion` 低于活动要求，升级配置值 |
| .zsb 导入解密失败 | 口令错误（错口令即失败无提示，是设计行为）；确认 KDF 迭代未被改 |
