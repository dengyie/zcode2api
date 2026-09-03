# 测试 01 — 测试策略总纲

## 1. 分层结构

```
            ┌─────────────────────────────┐
   少量     │  E2E（compose 全栈）         │  真实链路、发布前跑
            ├─────────────────────────────┤
            │  集成（Mock 上游 + 真进程）   │  故障注入矩阵、故障转移、调度器
            ├─────────────────────────────┤
            │  契约（上游响应 fixture）     │  上游结构变更的报警器
            ├─────────────────────────────┤
            │  对拍（interop vectors）     │  enc:v1 / .zsb 跨实现互通
            ├─────────────────────────────┤
   大量     │  单元（pytest，无 IO）        │  状态机/分类器/翻译器/编解码
            └─────────────────────────────┘
```

| 层 | 目录 | 依赖 | 运行时机 |
|----|------|------|----------|
| 单元 | `tests/unit/` | 无网络、无磁盘（临时目录/注入时间） | 每次提交 |
| 对拍 | `tests/interop/` | 固定向量 JSON | 每次提交 |
| 契约 | `tests/contract/` | `fixtures/upstream/*.json` | 每次提交 |
| 集成 | `tests/integration/` | Mock 上游进程 | 每次提交（CI 起 service） |
| E2E | `tests/e2e/` | docker compose 全栈 | 每日 + 发版前 |
| 冒烟 | `tests/smoke/`（脚本） | **真实上游**（只读操作） | 手动/发版前 |

## 2. 核心测试资产：Mock 上游

`tests/mock_upstream/` 是自建的 ZCode 上游仿真（FastAPI），这是整个测试体系的地基——三个来源项目的上游行为全部内化到这里。

**仿真端点**：OAuth（init/poll/token/z/login/userinfo）、`/api/v1/zcode-plan/anthropic/v1/messages`（SSE/批量）、`/api/anthropic/v1/messages`、billing（current/balance/preview/claim）、`client/configs`（startPlanPreview + captcha 配置）。

**故障注入**：请求头 `x-mock-scenario` 选择场景（完整矩阵见 04 文档 §2）；支持序列注入（`x-mock-sequence: rate_limited,ok`——第一次 429 第二次 200，用于验证故障转移后成功）。

**确定性**：SSE 帧固定序列（message_start → content_block_delta×N → message_delta(usage) → message_stop）；时间加速（claim 的退避窗可注入毫秒级）。

**保真纪律**：Mock 的响应结构必须来自 05 文档协议 + 三个来源项目实测抓包；一旦真实上游观测到新形态，先改 Mock + 契约 fixture，再修代码（契约测试会拦截遗漏）。

## 3. 可测性设计约束（架构向测试的让步）

- `pool.py`、`classify.py`、`translator/`、`bundle.py`、`zclient.enc_v1` **无 IO、时间注入**（`now: float | None = None` 参数），保证单元层无 mock 也能测。
- 所有上游 URL 走 `settings`（测试注入 Mock 地址），代码中不出现硬编码上游域名（`constants.py` 例外，且 CI 有断言测试扫描）。
- 验证码求解器在测试中统一替换为 `FakeCaptchaManager`（返回固定 verifyParam）；solver.js 本体用「桩可见性」测试（jsdom 桩函数齐全性）+ 真实环境手动验证。
- SQLite 用 `tmp_path` fixture；时间用 `freezegun` 或注入。

## 4. 覆盖率与门禁

| 指标 | 门禁 |
|------|------|
| 行覆盖（`app/` 总体） | ≥ 85% |
| 关键纯逻辑模块（pool/classify/translator/bundle/enc_v1/models 状态机） | ≥ 95% |
| 分支覆盖（classify.py、pool.py） | ≥ 90% |
| ruff + mypy-basic | 0 error |
| 互斥锁/并发用例（pool 单飞、claim 互斥） | 必须有 stress 用例（≥50 并发） |

CI（GitHub Actions）：push 跑 unit+interop+contract+integration（service 容器起 Mock）；每日定时跑 E2E；`phase/*` 分支额外跑覆盖率门禁。

## 5. 真实上游的使用纪律

- **冒烟只读**：`/v1/messages` 一条 8 token 请求、额度查询、claim preview——不做自动领取的真实上游自动化。
- 真实凭证不入 CI：环境变量注入，跑完即焚（`.zcode-hub-test-data/` 清理脚本断言）。
- 上游结构变更的发现路径：契约 fixture 版本化（`fixtures/upstream/v1/`），真实冒烟失败时先 diff fixture 再定位代码。

## 6. 缺陷防止目标（本测试体系最防的五类回归）

1. **换号风暴**：分类器误判（如把普通 400 判成额度耗尽）导致池内账号被连环标记 → CLASSIFY-* 用例矩阵 + 故障转移上限断言。
2. **额度误报**：billing 401（JWT 过期）与额度耗尽混淆 → QUOTA-401 vs QUOTA-EXHAUSTED 区分用例。
3. **加密互通破裂**：enc:v1/.zsb 字段序、padding、迭代参数漂移 → 对拍向量（03 文档）。
4. **SSE 流破坏**：翻译层/透传对分块边界的假设 → SSE 固定序列 + 随机分块（chunk 大小 1~64KB 随机化）。
5. **验证码死循环**：挑战未识别 → 无限原账号重试 → 重试上限 + 分类互斥断言（captcha 403 不计入 invalid）。
