# zcode-hub 文档中心

> zcode-hub：以 **zcode2api**（Python/FastAPI 多账号反代网关）为底座，融合 **zcode-switch**（ZCode 客户端账号切换器）与 **zcode-api**（zcode-proxy 网关）验证过的能力，形成的统一 ZCode 账号运营 + API 供给平台。
>
> **新项目，非 fork**。三个来源项目均为独立仓库，本仓库只移植代码与协议知识，并保留各自的许可证义务（zcode2api: AGPL-3.0 / zcode-switch: MIT / zcode-api: 无许可证，仅借鉴设计与协议，不复制代码）。

## 文档地图

| 文档 | 内容 | 读者 |
|------|------|------|
| **开发文档** ||
| [01-architecture.md](development/01-architecture.md) | 总体架构、模块职责、请求流程、状态机 | 全体 |
| [02-porting-map.md](development/02-porting-map.md) | 三方源码 → 新项目模块的移植映射（函数级对照） | 开发 |
| [03-data-formats.md](development/03-data-formats.md) | DB schema、凭证存储 v2、enc:v1 编解码、.zsb 包格式 | 开发 |
| [04-api-spec.md](development/04-api-spec.md) | 网关 API（Anthropic/OpenAI/Responses）+ 管理 API 规范 | 开发/调用方 |
| [05-upstream-protocols.md](development/05-upstream-protocols.md) | 上游协议参考：OAuth/对话/计费/领取/验证码/风控 | 开发 |
| [06-dev-guide.md](development/06-dev-guide.md) | 环境搭建、编码规范、测试运行、构建与部署（含 tebi 约定） | 开发 |
| [07-roadmap.md](development/07-roadmap.md) | 四阶段路线图、里程碑与依赖关系 | 全体 |
| **测试文档** ||
| [01-test-strategy.md](testing/01-test-strategy.md) | 测试策略总纲、分层、Mock 上游方案、覆盖门禁 | 全体 |
| [02-unit-tests.md](testing/02-unit-tests.md) | 分模块单元测试用例清单（给定 ID，可执行） | 开发 |
| [03-interop-vectors.md](testing/03-interop-vectors.md) | 加密对拍向量（enc:v1 / .zsb）与协议 fixture 规范 | 开发 |
| [04-integration-e2e.md](testing/04-integration-e2e.md) | 集成 / E2E 场景用例（含故障注入矩阵） | 开发 |
| [05-acceptance.md](testing/05-acceptance.md) | 验收标准与发布门禁清单 | 全体 |

## 来源项目

| 项目 | 角色 | 本地参考克隆 |
|------|------|--------------|
| [liu5269/zcode2api](https://github.com/liu5269/zcode2api) | 底座：FastAPI 网关 + 账号池 + 管理后台 | `/tmp/zcode2api`（可重新克隆） |
| [pjpv/zcode-switch](https://github.com/pjpv/zcode-switch) | 移植源：enc:v1 编解码 / 额度模型 / 活动领取 / .zsb 封包 | `/tmp/zcode-switch` |
| [TriDefender/zcode-api](https://github.com/TriDefender/zcode-api) | 设计参考：多格式网关 / 池化故障转移 / 风控对抗（已在 fork 中实现号池并部署验证） | `/Users/mango/project/zcode-api` |

## 快速导航

- 想理解系统怎么跑 → `development/01`
- 要写某个模块 → 先看 `development/02` 找到移植来源，再对照 `testing/02` 的用例清单写测试
- 碰到上游协议问题 → `development/05`
- 加密/封包互通问题 → `development/03` + `testing/03`
- 发版前 → `testing/05`
