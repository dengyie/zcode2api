"""账号数据模型与状态枚举。"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field

PROVIDERS = ("zai", "bigmodel")


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
    risk_strikes: int = 0  # 累计风控封禁次数（3012/405）；成功即清零
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

    def ban_for_risk(self) -> None:
        """命中真风控（3012/405「unusual activity」）：禁用账号，UI 展示封禁文案。

        风控由人工确认恢复后在后台手动启用（set_enabled）——不做自动退避恢复，
        避免对真封禁的账号持续产生上游流量。Plan 通道停用；同账号若有
        API Key 仍可走回退通道（is_selectable / uses_plan_channel 分离）。
        """
        self.risk_strikes += 1
        self.status = Status.DISABLED
        self.cooling_until = None

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
