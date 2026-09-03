# zcode-hub

**ZCode 账号运营 + 网关一体机** —— 在 zcode2api 底座上的全面扩展版（新项目，非 fork）。

融合三个上游项目的实证成果：

| 来源 | 关系 | 贡献 |
|------|------|------|
| [zcode2api](https://github.com/yuanhhs/zcode2api) (AGPL-3.0) | **底座**（代码级复制） | FastAPI 网关、SQLite 账号池、Node jsdom 验证码求解、OAuth |
| zcode-switch（MIT） | 移植目标（代码级） | enc:v1 解码、.zsb 封包互通、额度解析、客户端身份切换 |
| zcode-api | 设计参考（仅设计，无许可证，不复制代码） | 故障转移分类、防检测身份头、claim 协议认知 |

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # 按需修改
.venv/bin/python cli.py serve   # 默认 http://0.0.0.0:3000
```

- 后台管理：`http://127.0.0.1:3000/admin/login`（初始密码见 `.env` 的 `ZCODE_ADMIN_KEY`）
- 对话端点：`POST /v1/messages`（Anthropic Messages 协议，兼容 Claude Code）

CLI：

```bash
.venv/bin/python cli.py login zai            # OAuth 登录并自动入池
.venv/bin/python cli.py add-account zai 名称 <jwt|key>
.venv/bin/python cli.py quota                # 各账号实时额度
.venv/bin/python cli.py status
```

验证码求解（zai + JWT 账号必需）需要 Node：`cd captcha_node && npm install`。

## 开发

```bash
.venv/bin/python -m pytest            # 全量测试（Mock 上游，无真实网络）
.venv/bin/ruff check app tests
.venv/bin/mypy app/constants.py
```

测试不依赖真实上游：`tests/mock_upstream/` 模拟 zcode.z.ai 全部被依赖端点，
支持 `x-mock-scenario` 故障注入（见 `docs/testing/04`）。

## 文档

完整文档见 [docs/README.md](docs/README.md)：

- **开发**：[架构](docs/development/01-architecture.md) · [移植映射](docs/development/02-porting-map.md) · [数据格式](docs/development/03-data-formats.md) · [API 规范](docs/development/04-api-spec.md) · [上游协议](docs/development/05-upstream-protocols.md) · [开发指南](docs/development/06-dev-guide.md) · [路线图](docs/development/07-roadmap.md)
- **测试**：[策略](docs/testing/01-test-strategy.md) · [单元用例](docs/testing/02-unit-tests.md) · [互通向量](docs/testing/03-interop-vectors.md) · [集成 E2E](docs/testing/04-integration-e2e.md) · [验收](docs/testing/05-acceptance.md)

当前进度：**M0 骨架已完成**（底座落地 + 回归基线全绿），见路线图。

## 许可

AGPL-3.0（承袭底座 zcode2api）。
