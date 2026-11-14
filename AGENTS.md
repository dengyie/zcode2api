# AGENTS.md · zcode2api（zcode-hub）Agent 工作约定

面向在本仓工作的 AI agent / 贡献者。详细开发指南在 `docs/development/06-dev-guide.md`。

## 项目一句话

ZCode（Z.AI Coding Plan）账号运营 + Anthropic/OpenAI 协议反代一体机。
线上 = tebi 主机 `zcode-hub`（supervisord，127.0.0.1:8790，公网经 CF Tunnel `zcode.mangoqwq.com`）。
**pxed 是冻结回滚件，勿部署**。权威运维手册：Obsidian vault `Note/Infra/pxed zcode-hub 部署与运维`。

## 测试 / Lint

```bash
python -m pytest tests/ -q          # 全量（约 930+ 用例，必须全绿）
python -m pytest tests/unit -q      # 快速档
python -m ruff check app tests      # line-length 110，E501 忽略
```

- 上游常量只放 `app/constants.py`；配置旋钮走 `app/settings.py` 的 `_int/_bool` env 模式
- 错误分类唯一入口：`app/business_error.py` + `app/constants.py` 的业务码集合
  （裸子串匹配业务码是历史事故源——20 位 logid 陷阱，3009/1113/3007 三连教训）
- 注释里保留带日期的实证依据是本仓惯例，别删

## 发版 SOP

1. bump `app/settings.py` 的 `APP_VERSION`
2. 全量 pytest + ruff 绿
3. commit（格式 `feat|fix|test|docs|refactor|chore(scope): ...`）→ push GitHub `dengyie/zcode2api`
4. `scripts/deploy-backend.sh`（tebi，tar 管道 + 自动备份 + 原子换目录 + /meta 冒烟）
   或 `scripts/deploy-frontend.sh`（零重启）
5. 线上冒烟：`/meta` 版本号 + 一次真实 `/v1/messages`（回滚 = tebi `/data/zcode-hub/app.old` 换回 + restart）

## 已拍板语义（勿"顺手"改）

- `ACCOUNT_CONCURRENCY = 3`：对齐上游单账号并发硬限（2→10→3 纠正史见 settings.py 注释），
  env/meta 一律无效
- 领取/刷新节奏：quota 1800s、claim 轮 3600s（勿回 60s/600s）
- 3012/405 风控冷却、429 原地重试不冷却账号等错误策略见 `settings.py` 注释（2.6.8 口径）
- 验证码/身份头由本服务仿真，客户端透传不得覆盖（`agent._DROP_HEADERS`）

## ops/

`ops/` 下是 tebi 常驻运维 agent 的版本化正本（TetherPool 封禁换出口 / teleapi 实测探针），
同步方式见 `ops/README.md`。
