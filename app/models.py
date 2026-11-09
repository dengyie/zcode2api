"""账号数据模型与状态枚举。"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

PROVIDERS = ("zai", "bigmodel")


class Status:
    """账号运行状态。"""

    ACTIVE = "active"        # 正常，可参与轮询
    EXHAUSTED = "exhausted"  # 额度用完
    COOLING = "cooling"      # 临时限流（冷却中）
    INVALID = "invalid"      # 凭证失效 / 鉴权失败
    DISABLED = "disabled"    # 手动禁用


def expires_epoch(value) -> float | None:
    """把上游 `expires_at` 归一成 epoch 秒。

    两种形态都出现过：线上 accounts.db 里是 **epoch 秒**（如 1793375999），
    mock 与部分快照里是 **ISO8601 字符串**（`"2026-01-01T00:00:00Z"`）。原实现
    从不解析该字段（只看 remaining），所以「过期」从未被判过；写入归一函数后
    两种形态都能安全比较。兼容：epoch 秒、epoch 毫秒、ISO8601（带 Z / 偏移 /
    无时区按 UTC / 纯日期）、空值。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        return number / 1000.0 if number > 1e11 else number  # 毫秒时间戳
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        return number / 1000.0 if number > 1e11 else number
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def window_usable(window: dict, now: float) -> bool:
    """额度窗口是否还有效：剩余 > 0 **且**未过期。

    `remaining` 缺失视为「未知但有量」（上游有的窗口只给 total/used）；
    `expires_at` 缺失/无法解析视为不过期（一次性/永久池）。
    """
    if not isinstance(window, dict) or not window:
        return False
    remaining = window.get("remaining")
    if remaining is not None and (remaining or 0) <= 0:
        return False
    expires_at = expires_epoch(window.get("expires_at"))
    return expires_at is None or expires_at > now


def window_expired(window: dict, now: float) -> bool:
    """窗口是否已过有效期（只看 expires_at；无/不可解析 = 不过期）。"""
    if not isinstance(window, dict):
        return False
    expires_at = expires_epoch(window.get("expires_at"))
    return expires_at is not None and expires_at <= now


def _account_id(name: str) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in (name or "account").lower())
    safe = safe.strip("-")[:32] or "account"
    return f"{safe}-{secrets.token_hex(4)}"


@dataclass
class Account:
    """单个可轮询的账号凭证 + 运行时状态。"""

    id: str
    name: str
    provider: str
    mode: str  # "jwt" | "apiKey"
    jwt_token: str | None = None
    api_key: str | None = None
    # OAuth access_token（zcode.z.ai CLI OAuth 返回的 zai.access_token）：
    # 仅作信息留存（后台 has_oauth_token 标记，证明该号刚做过 OAuth 登录）。
    # **不是对话凭证**——api.z.ai 标准端点实测拒绝它（401），对话仍只走
    # JWT@zcode-plan 或 API Key@api.z.ai 两条真实通道。
    oauth_token: str | None = None
    enabled: bool = True
    status: str = Status.ACTIVE

    # 额度快照：{ model_show_name: {total, used, remaining, expires_at} }
    quota: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)        # 当前激活方案（billing/current plans[0]，兼容保留）
    plans: list = field(default_factory=list)       # 全部方案（上游 plans 数组；多套餐时 entitlements 不丢）
    usage: dict = field(default_factory=dict)       # 近期用量原始数据

    use_count: int = 0
    fail_count: int = 0
    risk_strikes: int = 0  # 当前风控退避周期内累计命中次数；成功或跨衰减窗口即清零
    last_risk_at: float | None = None  # 最近一次风控命中时间（strikes 衰减窗口基准）
    recent_results: list = field(default_factory=list)  # 最近请求结果 tick（True 成功/False 失败），最新在末尾
    last_used_at: float | None = None
    last_checked_at: float | None = None
    # 额度快照的「可信时间」：只在 billing/balance **成功取到数据**、quota 被真正
    # 写过的那一刻更新。与 last_checked_at（含失败/去抖，仅用于节流）区分开——
    # 过期判定只认这个（见 quota_expired：过期后还没成功查过 = 未知，不封号）。
    quota_verified_at: float | None = None
    cooling_until: float | None = None
    last_error: str | None = None
    created_at: float = field(default_factory=time.time)
    # 每账号客户端指纹（fingerprint.DeviceProfile；dataclass 存 dict，取用时还原）
    fingerprint: dict | object | None = None
    # 安装身份：入池分配的稳定安装令牌（hub 内部，跨账号不重复，导出时剥离）
    install_id: str | None = None
    installed_at: float | None = None  # 按账号安装序完成时间；None = 未安装

    @staticmethod
    def create(provider: str, name: str, secret: str) -> Account:
        secret = (secret or "").strip()
        is_jwt = secret.count(".") == 2 and provider == "zai"
        return Account(
            id=_account_id(name),
            name=name or f"{provider}-account",
            provider=provider,
            mode="jwt" if is_jwt else "apiKey",
            jwt_token=secret if is_jwt else None,
            api_key=None if is_jwt else secret,
        )

    @property
    def secret(self) -> str | None:
        return self.jwt_token if self.mode == "jwt" else self.api_key

    def risk_penalty(self, base: float, cap: float, ban_strikes: int,
                     decay_seconds: float) -> None:
        """命中风控（3012/405「unusual activity」）：指数退避冷却，累计
        ban_strikes 次才升级为禁用（人工确认恢复）。

        实测（2026-10-01）上游对 messages 端点的 3012 是频道级瞬时频控：
        同号同刻 billing 全部正常、数小时自愈——硬禁用会让健康的 billing/claim
        陪葬（领取轮两度因此停摆）。冷却自动恢复且冷却期零对话流量
        （is_selectable 挡住 messages 轮询），避免对真封禁账号持续施压；
        billing 链不设冷却闸（2026-11-04 起领取/查额度照跑，两条链互不搭桥）。

        strikes 仅在 decay_seconds 窗口内累计：距上次命中超过窗口视为无关的
        新事件，重新起算——避免跨月偶发命中累积成禁用。Plan 通道成功（gateway
        成功分支）或窗口衰减都会清零。
        """
        now = time.time()
        if self.last_risk_at and now - self.last_risk_at > decay_seconds:
            self.risk_strikes = 0
        self.risk_strikes += 1
        self.last_risk_at = now
        if self.risk_strikes >= ban_strikes:
            self.status = Status.DISABLED
            self.cooling_until = None
            return
        self.status = Status.COOLING
        self.cooling_until = now + min(base * (2 ** (self.risk_strikes - 1)), cap)

    def has_apikey_fallback(self) -> bool:
        """同账号是否持有可走 api.z.ai 的 API Key（JWT 死后的对话回退）。

        仅 JWT 账号的附加 Key 算回退；纯 apiKey 账号的主键不是 fallback，
        风控/失效后不得靠这把 Key 继续被选中。
        """
        return self.mode == "jwt" and bool((self.api_key or "").strip())

    def uses_plan_channel(self) -> bool:
        """是否允许走 Coding Plan JWT 对话通道（messages）。

        invalid / 风控 disabled / 手动停用 都视为对话通道不可用；有 Key 时
        对话走回退通道。**注意这只管「用」**——领取/查询额度看
        `allows_billing()`，两者互不搭桥（2026-11-04 用户口径）。
        """
        if self.mode != "jwt" or not (self.jwt_token or "").strip():
            return False
        if not self.enabled:
            return False
        return self.status not in (Status.INVALID, Status.DISABLED)

    def allows_billing(self, now: float | None = None) -> bool:
        """是否允许打 billing 全家桶（preview/claim/current/balance/usage）。

        **领取/查额度与「能不能发对话」是两条独立的链**（2026-11-04 用户口径）：
        上游 3012/unusual activity 只拦 messages，billing 族照常 200。因此
        风控禁用（DISABLED）、人工停放（enabled=False）、冷却中都不是理由——
        只要 JWT 还在（非 INVALID）就允许领取，避免「号停着就领不到投放」。
        代价是冷却期会多出 billing 读/写流量，这是刻意取舍。
        """
        if self.mode != "jwt" or not (self.jwt_token or "").strip():
            return False
        return self.status != Status.INVALID

    def quota_expired(self, now: float | None = None) -> bool:
        """权益是否已全部失效（每个额度窗口都「过期」或「剩余为 0」）。

        2026-10-31 线上实证：老号 `GLM-5.3-Flash` 本地记录 `remaining=1e8` 但
        `expires_at=10-03`（28 天前），上游一律回 `{"code":1005,"msg":"exceed
        quota limit"}` —— 只看 remaining 会把过期窗口当可用，等于让每个请求都
        先撞一次必然失败的 1005。

        口径：`quota` 为空 = **未知**（新号/尚未刷新），不判过期，否则会把可用
        账号挡在门外；只有取到记录且**每一个**窗口都不可用时才判 True。

        追加口径（2026-11-03）：额度窗口是**滚动日窗**，快照天然会过时——快照若
        取自最新到期时间**之前**，它没有资格判死。「有余额但全部过期」必须由
        **过期之后的一次成功查询**确认（`quota_verified_at > 最新 expires_at`），
        否则按未知处理、继续让账号参与轮询。实证：trial 号日窗
        `expires_at=11-02 23:59:59`，11-03 00:39 上游照回 200（新窗到 11-03
        23:59:59），而本地因 `quota_refresh_interval=0` 永不刷新，旧逻辑把能用的号
        永久封成 503。反例（要保住的行为）：老号 `expires_at` 在 28 天前、且过期后
        成功查过 → 仍判过期，不会每次请求先撞一次必然失败的 1005。
        """
        if not self.quota:
            return False
        now = now or time.time()
        if any(window_usable(w, now) for w in self.quota.values()):
            return False
        # 余额本身已归零 = 确定耗尽，与快照新旧无关（赠送池由调用方另判）
        if not any((w.get("remaining") or 0) > 0 for w in self.quota.values()):
            return True
        # 有余额、但每个窗口都过了期：可能只是快照停在上一轮日窗 → 需确认
        ends = [e for e in (expires_epoch(w.get("expires_at")) for w in self.quota.values()) if e]
        if not ends:
            return True
        verified = self.quota_verified_at
        return bool(verified and verified > max(ends))

    def record_result(self, ok: bool, detail: str = "", keep: int = 20) -> None:
        """记录单次请求结果明细（后台「最近请求」tick 悬停展示），只保留最近 keep 条。

        条目形如 {"ok": bool, "at": epoch 秒, "detail": 文案}；历史遗留的纯布尔条目
        由前端兼容渲染。
        """
        entry = {"ok": bool(ok), "at": time.time(), "detail": str(detail or ("成功" if ok else "失败"))}
        self.recent_results = (self.recent_results + [entry])[-keep:]

    def is_selectable(self, now: float | None = None) -> bool:
        """是否可被轮询选中。

        JWT 失效 / 风控禁用后，若同账号有 API Key，仍可选中并走回退通道；
        手动 enabled=False 永远不选。

        额度快照已判「全部窗口过期/为零」时也不选：这类账号本地看着有余额、
        上游只会回 1005，选中它等于每次请求都先烧一次必然失败的调用（且会
        挤掉真正可用的账号的轮询位）。额度未知（空快照）不在此列。
        """
        if not self.enabled:
            return False
        if self.status in (Status.INVALID, Status.DISABLED):
            # 回退通道（api.z.ai + API Key）花的是 Key 自己的额度，与套餐快照无关。
            return self.has_apikey_fallback()
        if self.status == Status.EXHAUSTED or self.quota_expired(now):
            return False
        if self.status == Status.COOLING:
            now = now or time.time()
            return bool(self.cooling_until and now >= self.cooling_until)
        return True

    def is_cooling(self, now: float | None = None) -> bool:
        """冷却是否仍在生效（含风控指数退避）。

        冷却期不该产生**对话（messages）**流量；billing 链（领取/查额度）不受
        此闸约束（2026-11-04 两条链解耦），因此不要把 is_cooling 塞进
        `allows_billing()`——它只用来挡 messages 选号与验证码池预热。
        """
        if self.status != Status.COOLING:
            return False
        now = now or time.time()
        return bool(self.cooling_until and now < self.cooling_until)

    def plan_kind(self) -> str:
        """当前套餐类型：start-plan（体验/免费档）/ coding-plan / ""（未知）。

        上游 plan_id 形如 `zcode-v3-start-plan-0817` / `zcode-v3-coding-plan-…`，
        取自 billing 的 plans[]（`quota.fetch_quota` 落库到 account.plans）。
        后台据此区分体验号与付费号；路由/限流如需按套餐分流也从这里取。
        """
        for plan in [self.plan, *(self.plans or [])]:
            if not isinstance(plan, dict):
                continue
            blob = " ".join(
                str(plan.get(k) or "").lower() for k in ("plan_id", "name", "description")
            )
            if "start-plan" in blob or "start plan" in blob:
                return "start-plan"
            if "coding-plan" in blob or "coding plan" in blob:
                return "coding-plan"
        return ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> Account:
        known = {f for f in Account.__dataclass_fields__}  # type: ignore[attr-defined]
        return Account(**{k: v for k, v in data.items() if k in known})

    def fingerprint_view(self) -> dict | None:
        """指纹的对外形态（dict）；未分配/半初始化返回 None。"""
        from .fingerprint import DeviceProfile

        fp = self.fingerprint
        if isinstance(fp, DeviceProfile):
            return {
                "platform": fp.platform, "arch": fp.arch,
                "os_version": fp.os_version, "language": fp.language,
                "timezone": fp.timezone, "screen": fp.screen,
                "device_mid": fp.device_mid,
            }
        if isinstance(fp, dict) and fp.get("device_mid"):
            return fp
        return None

    def public_view(self) -> dict:
        """返回给前端的视图（脱敏 token）。"""
        secret = self.secret or ""
        masked = secret if len(secret) <= 16 else f"{secret[:8]}…{secret[-6:]}"
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "mode": self.mode,
            "token_masked": masked,
            "enabled": self.enabled,
            "status": self.effective_status(),
            "quota": self.quota,
            "plan": self.plan,
            "plans": self.plans,
            "plan_kind": self.plan_kind(),
            "use_count": self.use_count,
            "fail_count": self.fail_count,
            "risk_strikes": self.risk_strikes,
            "recent_results": self.recent_results,
            "last_used_at": self.last_used_at,
            "last_checked_at": self.last_checked_at,
            "quota_verified_at": self.quota_verified_at,
            # 轮询是否会选中它（额度判死/冷却/停用都在这里体现）：后台排障时
            # 「状态 active 但不发请求」最常见的原因就是这一项为 false
            "selectable": self.is_selectable(),
            "cooling_until": self.cooling_until,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint_view(),
            "install_id": self.install_id,
            "installed_at": self.installed_at,
            # 是否持有 OAuth access_token（信息留存，不参与路由）
            "has_oauth_token": bool((self.oauth_token or "").strip()),
        }

    def effective_status(self, now: float | None = None) -> str:
        """考虑冷却到期后的实时状态。"""
        if self.status == Status.COOLING:
            now = now or time.time()
            if self.cooling_until and now >= self.cooling_until:
                return Status.ACTIVE
        return self.status
