"""进程级上游闸门（只作用于 zcode-plan 渠道）。

背景（2026-11，对齐 magpie `@magpie-community/opencode-zcode-auth` 实证）：

- 同一出口 IP 同时使用 3 个 ZCode 账号会被边缘 405 封地址（2 个不封）；
- Start Plan 约 18 请求/分钟、最多 10 在飞会触发 3008 连发，3–7 分钟后
  边缘 405 封 IP 1–2 小时（magpie 两日日志复盘：**重试能吸收 ~90% 的 3008**，
  上层完全无感、没人降速，地址就这么被打饱和）；
- 单账号并发由 store 的并发槽处理，但进程级没有「总速率 / 总在飞 / 边缘封禁」
  这层保护，风控会把整个出口 IP 上的所有账号一起带走。

本模块只做五件低风险的事，不参与账号选择、不改账号状态机：

1. 滑动分钟窗口内的 plan 渠道请求预算（0 = 关闭）；
2. plan 渠道全池在飞上限（0 = 关闭）；
3. 边缘封禁断路器：观察到 405 封 IP 页面后，全进程暂停 plan 渠道 N 秒；
4. 忙碌断路器（busy-hold，Magpie BUSY_N/BUSY_HOLD 同款）：3008 在短退避重试
   后**仍然** 3008（真饱和；一次重试就过去的瞬态争用不计）达到阈值，全进程
   暂停 plan 渠道一段窗口——封禁前先降速，而不是封了再认；
5. 套餐窗口闸门（pool-hold，2026-11-12）：全池账号都有「过期后实查确认枯竭」
   证据且无未来窗口时，快速拒绝 plan 渠道请求直到下一个已知窗口——套餐真空
   期（如 09:00 到期 → 19:00 放量）不再让每个请求白撞 5 轮账号×验证码求解。
   判定口径在 models.Account.next_window_start（保守优先：未知/陈旧一律放行），
   billing 刷新发现可用窗口即主动解除（quota.fetch_quota）。

api.z.ai / bigmodel 渠道不经过本闸门（key 渠道与 plan 渠道的风险面不同）。
"""

from __future__ import annotations

import math
import time

from . import settings


class Gate:
    def __init__(self) -> None:
        self._window_start = 0.0
        self._window_count = 0
        self._inflight = 0
        self._edge_until = 0.0
        self.edge_hits = 0
        self._busy_hits: list[float] = []
        self._busy_until = 0.0
        # 套餐窗口闸门（pool-hold）：与上面两个 hold 不同，锚的是**墙钟 epoch**
        # ——它必须与 billing 窗口的 expires_at / effective_at 对时，monotonic
        # 在这里是错的时钟。到期自动解除 + 额度恢复主动清除（quota.fetch_quota）
        self._pool_hold_until = 0.0  # wall-clock epoch

    # ── 边缘封禁 ──────────────────────────────────────────────────────────
    def edge_remaining(self) -> float:
        return max(0.0, self._edge_until - time.monotonic())

    def trip_edge(self, seconds: float | None = None) -> None:
        """记录一次边缘 405 封禁，并在窗口内挡住所有 plan 渠道请求。

        连续命中只延长窗口（max），不会因后来的短窗口截短长窗口。
        """
        secs = settings.GATE_EDGE_HOLD_SECONDS if seconds is None else seconds
        if secs <= 0:
            return
        self._edge_until = max(self._edge_until, time.monotonic() + secs)
        self.edge_hits += 1

    # ── 忙碌断路器（3008 真饱和）──────────────────────────────────────────
    def busy_remaining(self) -> float:
        return max(0.0, self._busy_until - time.monotonic())

    def note_busy(self) -> None:
        """记一次「重试后仍 3008」的饱和信号；窗口内累计到阈值即触发忙碌断路器。

        只该由「同一请求短退避重试后再次 3008」调用——一次重试就过去的瞬态
        争用是正常排队，计进来会把正常并发抖动误判成饱和（magpie 0.1.55
        教训：每个被 capped 的请求都计数，忙时把用户白白冻结）。
        """
        threshold = settings.GATE_BUSY_THRESHOLD
        window = settings.GATE_BUSY_WINDOW_SECONDS
        hold = settings.GATE_BUSY_HOLD_SECONDS
        if threshold <= 0 or hold <= 0:
            return
        now = time.monotonic()
        self._busy_hits = [t for t in self._busy_hits if now - t < window]
        self._busy_hits.append(now)
        if len(self._busy_hits) >= threshold:
            self._busy_until = max(self._busy_until, now + hold)
            self._busy_hits.clear()

    # ── 套餐窗口闸门（pool-hold）──────────────────────────────────────────
    def pool_hold_remaining(self, now: float | None = None) -> float:
        return max(0.0, self._pool_hold_until - (now if now is not None else time.time()))

    def note_pool_exhausted(self, next_start: float, now: float | None = None) -> None:
        """全池确认枯竭后暂停 plan 渠道，直到下一个已知窗口（或上限兜底）。

        next_start 是全池最小的未来 effective_at（epoch 秒）；-1 = 有枯竭证据
        但不知何时恢复，直接用 GATE_POOL_HOLD_MAX_SECONDS 兜底——宁可早放行
        重探一次，也不能把恢复后的窗口吊死在本地闸门里。
        """
        cap = settings.GATE_POOL_HOLD_MAX_SECONDS
        if cap <= 0:
            return
        now = now if now is not None else time.time()
        if next_start is not None and next_start > now:
            hold = min(next_start - now, float(cap))
        else:
            hold = float(cap)
        self._pool_hold_until = max(self._pool_hold_until, now + hold)

    def clear_pool_hold(self) -> None:
        """billing 刷新发现可用窗口时解除（quota.fetch_quota 恢复分支调用）。"""
        self._pool_hold_until = 0.0

    # ── 准入 ─────────────────────────────────────────────────────────────
    def hold_reason(self) -> str | None:
        """当前生效的进程级 hold；无则 None。

        优先级：边缘封禁 > 忙碌断路器 > 套餐窗口。前两个是异常保护（IP 被封/
        真饱和），最后一个只是日常真空期的快速失败——都被挡时回异常的那个，
        Retry-After 才有信息量。
        """
        if self.edge_remaining() > 0:
            return "edge_hold"
        if self.busy_remaining() > 0:
            return "busy_hold"
        if self.pool_hold_remaining() > 0:
            return "pool_hold"
        return None

    def _rate_refused(self) -> bool:
        """分钟预算是否已用尽（窗口过期不算——acquire 会在下一次重置）。"""
        if settings.GATE_RATE_PER_MIN <= 0:
            return False
        if time.monotonic() - self._window_start >= 60:
            return False
        return self._window_count >= settings.GATE_RATE_PER_MIN

    def refusal_reason(self) -> str | None:
        """当前会被 acquire 拒绝的原因（hold 优先，其次分钟预算）；可通行则 None。

        供调度兜底与验证码前置短路使用：hold 与预算都是进程级状态，与具体
        账号无关——被拒时换账号同样会被拒，应在调度层一次性行为化。
        """
        hold = self.hold_reason()
        if hold is not None:
            return hold
        if self._rate_refused():
            return "rate_limit"
        return None

    def acquire(self) -> tuple[bool, str]:
        """尝试占用一个 plan 渠道槽位。返回 (ok, reason)。"""
        hold = self.hold_reason()
        if hold is not None:
            return False, hold
        if self._rate_refused():
            return False, "rate_limit"

        cap = settings.GATE_MAX_INFLIGHT
        if cap > 0 and self._inflight >= cap:
            return False, "inflight"

        rate = settings.GATE_RATE_PER_MIN
        if rate > 0:
            now = time.monotonic()
            if now - self._window_start >= 60:
                self._window_start = now
                self._window_count = 0
            if self._window_count >= rate:
                return False, "rate_limit"
            self._window_count += 1

        self._inflight += 1
        return True, ""

    def release(self) -> None:
        if self._inflight > 0:
            self._inflight -= 1

    def retry_after(self) -> int:
        """给客户端的 Retry-After（秒），按当前拒绝原因估算。"""
        edge = self.edge_remaining()
        if edge > 0:
            return max(1, math.ceil(edge))
        busy = self.busy_remaining()
        if busy > 0:
            return max(1, math.ceil(busy))
        pool = self.pool_hold_remaining()
        if pool > 0:
            return max(1, math.ceil(pool))
        now = time.monotonic()
        if settings.GATE_RATE_PER_MIN > 0 and self._window_start > 0 and now - self._window_start < 60:
            return max(1, math.ceil(60 - (now - self._window_start)))
        return 1

    def reset(self) -> None:
        """测试/热重载用：清空全部计数与断路器。"""
        self._window_start = 0.0
        self._window_count = 0
        self._inflight = 0
        self._edge_until = 0.0
        self.edge_hits = 0
        self._busy_hits = []
        self._busy_until = 0.0
        self._pool_hold_until = 0.0


gate = Gate()
