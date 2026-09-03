# 测试 03 — 互通对拍向量与 Fixture 规范

状态：enc:v1 向量已确认（来自 zcode-switch 官方测试向量）；.zsb 向量待 M0 回填 envelope 后生成。

## 1. enc:v1 对拍向量

### 1.1 zcode-switch 官方向量（必须原样内置）

文件：`tests/interop/vectors/enc-v1/zsw-node-enc-v1.json`

```json
{
  "secret": "zsw-node-vector-secret-2026",
  "enc": "enc:v1:MjAyNjA4MjN6c3cw.pttsIHEYcbW0bz5jD4XGOw.wFBGfsU8jgZUcB8xe6Edh6Vq",
  "plain": "hello zcode-switch",
  "note": "node crypto aes-256-gcm cross-language vector, generated 2026-08-23"
}
```

断言：`decrypt_with_secret(enc, secret) == plain`。这是 ZC-001 的数据源。

### 1.2 我方向量集（M3 生成，双向）

`tests/interop/vectors/enc-v1/zcode-hub-generated.json`：

```json
{
  "vectors": [
    { "case": "ascii",         "secret": "s1", "plain": "hello" },
    { "case": "unicode",       "secret": "密钥", "plain": "中文内容🔐" },
    { "case": "long-secret",   "secret": "<4096 字符>", "plain": "x" },
    { "case": "long-plain",    "secret": "s", "plain": "<1MB 随机 UTF-8>" },
    { "case": "empty-plain",   "secret": "s", "plain": "" }
  ],
  "ciphertexts": "<由本实现生成后固化；任何重加密不得改动本文件，只增不改>"
}
```

用途：固化本实现的加密输出，防止字段序/padding 语义漂移；同时提供给 zcode-switch 侧做互解验证（其 `#[cfg(test)] encrypt_with_secret` + `decrypt_with_secret` 可直接消费）。

### 1.3 secret 派生矩阵向量

`tests/interop/vectors/enc-v1/derive-matrix.json` — 覆盖：

| platform | username 来源 | 期望 secret |
|----------|---------------|-------------|
| win32 | USERNAME=alice | `zcode-credential-fallback:win32:{home}:alice` |
| linux | id -un → mango | `zcode-credential-fallback:linux:{home}:mango` |
| darwin | USER=bob | `zcode-credential-fallback:darwin:{home}:bob` |
| linux | 无任何来源 | `zcode-credential-fallback:linux:{home}:unknown` |

`{home}` 用测试临时目录注入（派生只关心字符串拼接本身）。ZC-003 消费。

### 1.4 运行方式

```bash
pytest tests/interop/test_enc_v1.py -q
# 实现位置：app/zclient.py；算法要点见开发文档 03 §3
```

## 2. .zsb 互通向量（M1 产出）

### 2.1 前置回填任务（M0）

从 zcode-switch `src-tauri/src/cipher.rs`（110 行，全文通读）确认 envelope 完整 schema，回填开发文档 03 §4。重点确认：nonce/salt/ciphertext 字段名与编码、payload 明文的账号数组结构、可能的版本号/checksum 字段。

### 2.2 向量生成流程

1. **上游参照物**：本地 cargo 跑 zcode-switch（`cd /tmp/zcode-switch/src-tauri && cargo test`），用其测试工具或临时 `#[cfg(test)]` 导出函数生成固定输入的 .zsb（口令 `zsw-interop-test-2026`）。
2. 固化三个向量到 `tests/interop/vectors/zsb/`：
   - `single-account.zsb`：单账号（zai, jwt 模式）
   - `multi-account.zsb`：三账号混合（zai×2 + bigmodel×1）
   - `legacy-fields.zsb`：含我方不使用的额外字段（验证导入只取所需、忽略未知）
3. 断言（BN-005）：
   - 我方 `bundle.import` 三个文件 → 账号数/凭证值逐字段一致
   - 我方 `bundle.export`（同输入、同口令）→ 由 zsw `import_values`（`store.rs` 测试路径）导入成功
   - 注意：GCM 随机 nonce 导致密文不可复现，**互通断言只对"可导入且解出一致"负责，不对密文逐字节一致负责**

### 2.3 负向向量

| 文件 | 场景 | 断言 |
|------|------|------|
| `wrong-password.zsb` | 用向量口令生成后以错口令导入 | 失败，无明文泄漏（内存清扫不作为断言） |
| `tampered.zsb` | ciphertext 改一字节 | GCM 校验失败 |
| `bad-kdf-iters.zsb` | iters 改 1000 | 拒绝（互通参数锁定 100000） |
| `truncated.zsb` | 截断 10 字节 | 结构性失败，明确报错 |

## 3. 上游响应 Fixture（契约层）

`tests/contract/fixtures/upstream/v1/` — Mock 上游与契约测试共用，来源标注抓包出处：

| fixture | 内容 | 出处 |
|---------|------|------|
| `client-configs.json` | startPlanPreview（GLM-5.3 3M / Turbo 2M）+ captcha 配置 | 逆向文档实测 2026-08-18 |
| `billing-balance.json` | `data.balances[]`（show_name/total/used/remaining/expires_at） | z2a quota 解析路径 |
| `billing-current.json` | `data.plans[]`（plan_id/status/units） | 同上 |
| `subscription-list.json` | `{"code":200,"data":[]}`（免费用户）与含套餐两版 | 账号侧验证记录 |
| `quota-limit-free.json` | `{"code":500,"msg":"当前用户不存在coding plan"}`（免费用户语义） | 同上 |
| `claim-preview-live.json` / `claim-preview-empty.json` | ClaimPlan 数组 / 活动未部署 | zsw claim.rs 结构 |
| `claim-*.json` | already_claimed / quota_exhausted / ineligible 响应 | zsw 退避路径 |
| `messages-sse-frames.json` | SSE 帧序列（message_start → delta×N → usage → stop） | z2a/zapi 透传观测 |
| `messages-error-402.json` / `-429.json` / `-401.json` / `-403-captcha.json` / `-400-3007.json` | 错误体原文 | 分类器数据源 |
| `oauth-init.json` / `oauth-poll-done.json` / `z-login.json` / `customer-info.json` / `api-keys.json` | OAuth 链路各步 | z2a oauth.py + zsw oauth.rs |

**契约测试**（`tests/contract/`）断言两件事：① Mock 上游返回与 fixture 逐字段一致；② 我方解析器对 fixture 的解析结果稳定（金样本断言）。真实上游冒烟若失败 → 先 diff 此层。

## 4. 向量维护规则

- 向量文件**只增不改**：修正错误向量需新增版本文件（`*-v2.json`）并废弃旧的，PR 中说明原因。
- 任何触碰 `zclient.enc_v1` / `bundle` 的 PR 必须附对拍运行输出。
- zcode-switch 上游若更新向量（其仓库 CI 或 release notes），同步跟进并跑兼容回归。
