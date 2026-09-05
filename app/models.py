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
    plan: dict = field(default_factory=dict)        # 当前激活方案
    usage: dict = field(default_factory=dict)       # 近期用量原始数据

    use_count: int = 0
    fail_count: int = 0
    risk_strikes: int = 0  # 连续风控（3012/405）命中次数，用于指数退避；成功即清零
    cooling_is_risk: bool = False  # 当前 COOLING 是否由风控引起（区分 429/断连冷却，并发去重用）
    last_used_at: float | None = None
    last_checked_at: float | None = None
    cooling_until: float | None = None
    last_error: str | None = None
    created_at: float = field(default_factory=time.time)

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

    def begin_risk_cooldown(self, base: int, cap: int, now: float | None = None) -> int:
        """命中风控：累加连续计数并按指数退避设置冷却截止，返回冷却秒数。

        第 n 次连续命中冷却 min(base * 2^(n-1), cap) 秒。
        已处于风控冷却中（并发在途请求同批收到风控响应）视为同一事故：
        不叠加连击、只顺延冷却截止 —— 否则几个并发请求就能把一次事故推到封顶。
        """
        now = now or time.time()
        if self.is_cooling(now) and self.cooling_is_risk:
            tier = min(base * (2 ** (self.risk_strikes - 1)), cap)
            self.cooling_until = now + tier
            return tier
        self.risk_strikes += 1
        cooldown = min(base * (2 ** (self.risk_strikes - 1)), cap)
        self.status = Status.COOLING
        self.cooling_until = now + cooldown
        self.cooling_is_risk = True
        return cooldown

    def is_selectable(self, now: float | None = None) -> bool:
        """是否可被轮询选中。"""
        if not self.enabled or self.status in (Status.DISABLED, Status.INVALID):
            return False
        if self.status == Status.EXHAUSTED:
            return False
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
            "use_count": self.use_count,
            "fail_count": self.fail_count,
            "risk_strikes": self.risk_strikes,
            "last_used_at": self.last_used_at,
            "last_checked_at": self.last_checked_at,
            "cooling_until": self.cooling_until,
            "last_error": self.last_error,
            "created_at": self.created_at,
        }

    def effective_status(self, now: float | None = None) -> str:
        """考虑冷却到期后的实时状态。"""
        if self.status == Status.COOLING:
            now = now or time.time()
            if self.cooling_until and now >= self.cooling_until:
                return Status.ACTIVE
        return self.status
