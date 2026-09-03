# 03 — 数据格式与存储规范

状态：**定稿**。enc:v1 / SQLite / `.zsb` envelope 全字段清单均已按源码核对（.zsb 于 2026-09-03 从 zsw `cipher.rs` 全文 + `store.rs` 回填）。

## 1. SQLite Schema（`data/zcode-hub.db`，WAL）

```sql
-- 账号池
CREATE TABLE accounts (
    id          TEXT PRIMARY KEY,          -- zai-<sha256[:12]>（按 provider+凭证派生，重登稳定）
    provider    TEXT NOT NULL,             -- 'zai' | 'bigmodel'
    name        TEXT,
    label       TEXT,                      -- 用户可读标签（login --label）
    mode        TEXT NOT NULL,             -- 'jwt' | 'apiKey'
    status      TEXT NOT NULL,             -- active|exhausted|cooling|invalid|disabled
    enabled     INTEGER NOT NULL DEFAULT 1,
    cooling_until REAL,                    -- unix 秒
    created_at  REAL,
    data        TEXT NOT NULL              -- JSON：凭证 + 运行时统计（见下）
);
CREATE INDEX idx_acc_provider ON accounts(provider);
CREATE INDEX idx_acc_status   ON accounts(status);

-- data JSON 字段（Account.to_dict()）
-- { jwt_token?, api_key?, api_secret?, user_id?, quota:{model:{total,used,remaining,expires_at}},
--   plan_slots:[...], plan: {...}, use_count, fail_count, last_used_at, last_checked_at, last_error }

-- 设置 KV（admin_key / gateway_key / 各 interval）
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- 领取历史
CREATE TABLE claim_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL,
    plan_id     TEXT, plan_name TEXT,
    outcome     TEXT NOT NULL,             -- claimed|already_claimed|quota_exhausted|failed
    starts_at   REAL, ends_at   REAL,      -- 套餐生效窗口
    detail      TEXT,                      -- 上游原始响应摘要
    created_at  REAL NOT NULL
);
```

设计要点（继承 z2a 并扩展）：

- **内存驻留 + 落库同步**：运行期账号对象常驻内存保证轮询游标与状态实时性，每次变更 `INSERT OR REPLACE` 落库；启动时读快照重建（z2a `store.py` 语义）。
- 凭证字段在 `data` JSON 内**加密存储**（AES-256-GCM，主密钥来自 `ZCODE_MASTER_SECRET` env；缺省派生方式同 enc:v1 的 fallback 思路，文档化并可在测试中固定）。这是对 z2a 明文存储缺陷的修复。
- `public_view()` 一律脱敏：`{key[:8]}…{key[-6:]}`。

## 2. 运行时配置（settings）

| 变量 | 默认 | 说明 |
|------|------|------|
| `ZCODE_PORT` / `ZCODE_HOST` | 3000 / 0.0.0.0 | 服务监听 |
| `ZCODE_ADMIN_KEY` | `zcode` | 后台密码初值（之后以 DB meta 为准） |
| `ZCODE_GATEWAY_KEY` | 空 | 网关 API Key（空 = 不校验，仅限本机使用） |
| `ZCODE_MASTER_SECRET` | 派生 | 账号凭证加密主密钥 |
| `ZCODE_DATA_DIR` | `./data` | SQLite 与凭证目录 |
| `POOL_MAX_ATTEMPTS` | 4 | 单请求内换号上限 |
| `POOL_COOLDOWN_SECONDS` | 300 | 429 冷却 |
| `POOL_EXHAUSTED_RETRY_SECONDS` | 1800 | 额度耗尽重试窗口 |
| `QUOTA_REFRESH_INTERVAL` | 60 | 额度轮询间隔（0 关闭） |
| `QUOTA_STAGGER_MAX_MS` | 8000 | 错峰抖动上限 |
| `CLAIM_ENABLED` / `CLAIM_AUTO` | false / true | 领取开关 / 后台自动 |
| `CLAIM_POLL_INTERVAL` / `CLAIM_COOLDOWN` | 300s / 600s | 领取轮询 / 失败退避 |
| `ZAI_UPSTREAM_URL` / `ZAI_FALLBACK_URL` / `BIGMODEL_UPSTREAM_URL` | 官方端点 | 上游可覆写（测试注入用） |
| `ZCODE_NODE_PATH` / `ZCODE_CAPTCHA_TIMEOUT` / `ZCODE_CAPTCHA_RETRIES` / `CAPTCHA_CACHE_TTL` | node / 40s / 4 / 45000ms | 验证码求解 |

## 3. enc:v1 编解码（ZCode 客户端凭证格式，zsw zcrypto.rs）

用于：读取/写回本机 ZCode 客户端 `~/.zcode/v2/credentials.json`（Phase 3）。

```
字符串形态:  enc:v1:{nonce_b64}.{tag_b64}.{ct_b64}
base64:     URL_SAFE_NO_PAD
nonce:      12 字节随机
tag:        GCM tag，16 字节（注意：加密输出时从密文尾部切 16 字节，验证时拼回 ct 尾部）
算法:       AES-256-GCM，key = SHA256(secret)（32 字节，无 KDF 迭代）
secret 默认: "zcode-credential-fallback:{platform}:{home}:{username}"
             platform ∈ {win32, darwin, linux}   （node os.platform() 语义）
             username 解析顺序: USERNAME → (非Windows) id -un → USER → LOGNAME → "unknown"
env 覆盖:    ZCODE_CREDENTIAL_SECRET
```

Python 参考实现（`zclient.py`，须通过对拍向量）：

```python
import base64, hashlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"

def derive_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()

def decrypt_with_secret(value: str, secret: str) -> str:
    body = value.removeprefix(PREFIX)
    n_b64, t_b64, c_b64 = body.split(".")          # 恰好三段
    nonce  = base64.urlsafe_b64decode(n_b64 + "==")
    tag    = base64.urlsafe_b64decode(t_b64 + "==")
    ct     = base64.urlsafe_b64decode(c_b64 + "==")
    assert len(nonce) == 12
    return AESGCM(derive_key(secret)).decrypt(nonce, ct + tag, None).decode()

def encrypt_with_secret(plain: str, secret: str) -> str:
    nonce = os.urandom(12)
    sealed = AESGCM(derive_key(secret)).encrypt(nonce, plain.encode(), None)
    ct, tag = sealed[:-16], sealed[-16:]
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return f"{PREFIX}{b64(nonce)}.{b64(tag)}.{b64(ct)}"
```

> 实现注意：`urlsafe_b64decode` 需补齐 `=` padding；`extra=None`（ZCode 客户端无 AAD）。

### ZCode 客户端相关文件（Phase 3 快照/切换对象）

| 文件 | 内容 | 操作 |
|------|------|------|
| `~/.zcode/v2/credentials.json` | 登录凭证 JSON，敏感字段形如 `enc:v1:...`（含 `zcodejwttoken`） | 快照 / 原子写回 |
| `~/.zcode/v2/config.json` | provider 配置（`builtin:zai-coding-plan` 等，options.apiKey） | 快照 / 原子写回 |
| `~/.zcode/v2/telemetry-state.json` | 遥测状态 | 随账号一并快照 |
| 账号库目录 | `~/.zcode-switch/accounts/{id}/`（zsw 约定，我们读写兼容） | 快照存放；id 白名单 `[A-Za-z0-9-]` |

## 4. `.zsb` 加密封包（与 zcode-switch 互通）

设计目标：**zcode-hub 导出的 .zsb 能被 zcode-switch 导入，反之亦然。**

> 状态：**已回填定稿**（2026-09-03，源码：zsw `cipher.rs` 全文 110 行 + `store.rs` `export_bundle_value` L777 / `import_candidates` L794 / Account 结构 L82 / `capture_current` L448）。

### 4.1 外层 envelope（加密层，cipher.rs）

`.zsb` 文件本体是一个 JSON 对象，顶层字段恰好三个：`format` / `version` / `kdf` / `cipher`（四个）：

```json
{
  "format": "zcode-accounts-bundle",
  "version": 1,
  "kdf":    { "algo": "pbkdf2-hmac-sha256", "iters": 100000, "salt": "<STD b64, 16 字节 salt>" },
  "cipher": { "algo": "aes-256-gcm", "nonce": "<STD b64, 12 字节>", "tag": "<STD b64, 16 字节>", "data": "<STD b64>" }
}
```

要点（全部来自 `cipher.rs` 逐行核对）：

- 常量：`FORMAT_BUNDLE = "zcode-accounts-bundle"`，`KDF_ITERS = 100_000`；envelope 的 `version` 恒为 `1`。
- 派生：`key = PBKDF2-HMAC-SHA256(password, salt, 100000, 32B)`，salt 由 OsRng 随机 16 字节。
- 加密：AES-256-GCM，随机 12 字节 nonce；Rust `aes_gcm` 的 `encrypt` 返回 `ciphertext || tag`，zsw 用 `split_at(ct.len()-16)` 把它拆成 `data`（密文本体）与 `tag`（认证标签）**分开存储**。
- base64：`base64::engine::general_purpose::STANDARD` = **标准字母表 + padding**（与 enc:v1 的 URL_SAFE_NO_PAD 不同！Python 侧直接用 `base64.b64encode/b64decode` 即可）。
- 解密侧校验顺序：`kdf`→`cipher` 字段存在性 → salt/nonce/tag/data 可解码 → **nonce 必须 12 字节** → 派生 key → `data || tag` 一起喂给 GCM `decrypt` → 明文必须能 JSON 反序列化。密码错误报 `wrong_password`（这是 zsw UI 判定「密码不对」的唯一依据，我们的实现要保留同语义）。
- `is_sealed()` 判定：对象同时含 `kdf` 与 `cipher` 字段即视为已加密信封。
- 口令：trim 后为空即拒绝 seal（open 侧无此校验，空口令可解密空口令封的包）。

### 4.2 内层明文 payload（store.rs）

`open()` 解出的明文是 `export_bundle_value()` 的 JSON 序列化（`serde_json::to_vec`）：

```json
{
  "format": "zcode-accounts-bundle",
  "version": 2,
  "exportedAt": "2026-09-03T12:00:00+08:00",
  "accounts": [
    {
      "name": "账号显示名",
      "createdAt": "2026-09-01T10:00:00+08:00",
      "credentials": { "oauth:zai:access_token": "enc:v1:...", "zcodejwttoken": "enc:v1:..." },
      "config": { "provider": { "builtin:zai-coding-plan": { "options": { "apiKey": "..." } } } }
    }
  ]
}
```

要点：

- **内层 `version` 是 2，外层 envelope `version` 是 1** —— 两个版本号不通用，互导判定时分开看。
- `accounts[]` 每项四字段：`name` / `createdAt` / `credentials` / `config`（`config` 可为 null，zsw 导入时 `config: null` 允许通过）。注意内层**不含** `id`/`hash`/`updatedAt`/`virtual_device_mid` —— 这些是 zsw 本地账号库的私有字段，导出时被有意剥离。
- `credentials` 是 `~/.zcode/v2/credentials.json` 的原样内容（`capture_current` 直接存 live 文件），敏感值是 `enc:v1:` 密文；`config` 是 `config.json` 原样内容或 null。
- 导入识别：`import_candidates()` 只认 `format == "zcode-accounts-bundle"`，其余（含旧版单账号 `zcode-account`、裸 credentials.json）报「无法识别」拒绝——**不要**为了宽容而放宽这个判定，否则 03 测试文档的负向向量 BN-002/003 会失效。
- 时间戳：zsw 用本地时区 ISO8601（chrono `Local`），我们保持 ISO8601 即可，导入方不校验格式。
- zcode-hub 侧导入只取 `credentials` 中的可解字段（provider/apiKey/secret/jwt/name），多余字段忽略（同 zsw 行为）。

### 4.3 口令来源

- CLI `--password` 或环境变量 `ZSW_PASSWORD`（沿用 zsw 命名保持脚本兼容；zcode-hub 自身另支持 `ZCODE_BUNDLE_PASSWORD`，`ZSW_PASSWORD` 优先）。

## 5. 运行时产物

| 产物 | 位置 | 生命周期 |
|------|------|----------|
| `accounts.db` (+wal/shm) | `$ZCODE_DATA_DIR` | 常驻，备份对象 |
| 验证码缓存 | 进程内存 | TTL 45s，进程重启即失 |
| claim_history | SQLite 表 | 永久，UI 展示 |
| 日志 | stdout（supervisor/docker 接管） | 滚动由部署层负责 |
