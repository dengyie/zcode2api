"""账号数据模型与状态枚举。"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field

PROVIDERS = ("zai", "bigmodel")
# 账号区域：intl=国际版(z.ai 授权，业务域 api.z.ai)；cn=国内版(bigmodel.cn 授权，
# 业务域 bigmodel.cn)。Plan 通道（messages/billing/claim）两区域同端点，JWT 决定归属
REALMS = ("intl", "cn")


class Status:
    """账号运行状态。"""

    ACTIVE = "active"        # 正常，可参与轮询
    EXHAUSTED = "exhausted"  # 额度用完
    COOLING = "cooling"      # 临时限流（冷却中）
    INVALID = "invalid"      # 凭证失效 / 鉴权失败
    DISABLED = "disabled"    # 手动禁用


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
    realm: str = "intl"  # 账号区域（models.REALMS）：intl=国际版 / cn=国内版
    jwt_token: str | None = None
    api_key: str | None = None
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
    cooling_until: float | None = None
    last_error: str | None = None
    created_at: float = field(default_factory=time.time)
    # 每账号客户端指纹（fingerprint.DeviceProfile；dataclass 存 dict，取用时还原）
    fingerprint: dict | object | None = None
    # 安装身份：入池分配的稳定安装令牌（hub 内部，跨账号不重复，导出时剥离）
    install_id: str | None = None
    installed_at: float | None = None  # 按账号安装序完成时间；None = 未安装

    @staticmethod
    def create(provider: str, name: str, secret: str, realm: str = "") -> Account:
        secret = (secret or "").strip()
        is_jwt = secret.count(".") == 2 and provider == "zai"
        # 区域缺省/非法值时自动归区：bigmodel 提供商即国内域 Key，其余国际版
        if realm not in REALMS:
            realm = "cn" if provider == "bigmodel" else "intl"
        return Account(
            id=_account_id(name),
            name=name or f"{provider}-account",
            provider=provider,
            mode="jwt" if is_jwt else "apiKey",
            realm=realm,
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
        陪葬（领取轮两度因此停摆）。冷却自动恢复且冷却期零上游流量
        （is_cooling 全通道门禁），既避免对真封禁账号持续施压，也不拖死领取轮。

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
        """当前是否允许走 Coding Plan JWT 通道（messages + billing）。

        invalid / 风控 disabled / 手动停用 都视为 JWT 不可用；有 Key 时对话
        走回退通道，但 billing/claim 仍必须停（Key 通道没有套餐领取）。
        """
        if self.mode != "jwt" or not (self.jwt_token or "").strip():
            return False
        if not self.enabled:
            return False
        return self.status not in (Status.INVALID, Status.DISABLED)

    def allows_billing(self, now: float | None = None) -> bool:
        """是否允许打 billing 全家桶（preview/claim/current/balance/usage）。"""
        return self.uses_plan_channel() and not self.is_cooling(now)

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
        """
        if not self.enabled:
            return False
        if self.status == Status.EXHAUSTED:
            return False
        if self.status in (Status.INVALID, Status.DISABLED):
            return self.has_apikey_fallback()
        if self.status == Status.COOLING:
            now = now or time.time()
            return bool(self.cooling_until and now >= self.cooling_until)
        return True

    def is_cooling(self, now: float | None = None) -> bool:
        """冷却是否仍在生效（含风控指数退避）。冷却期内不应产生任何上游流量。"""
        if self.status != Status.COOLING:
            return False
        now = now or time.time()
        return bool(self.cooling_until and now < self.cooling_until)

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
            "realm": self.realm,
            "mode": self.mode,
            "token_masked": masked,
            "enabled": self.enabled,
            "status": self.effective_status(),
            "quota": self.quota,
            "plan": self.plan,
            "plans": self.plans,
            "use_count": self.use_count,
            "fail_count": self.fail_count,
            "risk_strikes": self.risk_strikes,
            "recent_results": self.recent_results,
            "last_used_at": self.last_used_at,
            "last_checked_at": self.last_checked_at,
            "cooling_until": self.cooling_until,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint_view(),
            "install_id": self.install_id,
            "installed_at": self.installed_at,
        }

    def effective_status(self, now: float | None = None) -> str:
        """考虑冷却到期后的实时状态。"""
        if self.status == Status.COOLING:
            now = now or time.time()
            if self.cooling_until and now >= self.cooling_until:
                return Status.ACTIVE
        return self.status
