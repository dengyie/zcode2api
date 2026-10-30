"""验证码求解 + 预解 token 池。

主求解器 solver_pw.js：真浏览器（puppeteer-core + 系统 Chromium）运行阿里云
官方无痕 SDK 求 verifyParam（X-Aliyun-Captcha-Verify-Param）——阿里云无痕
验证信任真实浏览器环境，happy-dom 伪造路线 2026-09 起被风控「unusual
activity」全拒（solver.js 仅作 ZCODE_CAPTCHA_SOLVER=legacy 回滚）。

架构对齐 zapi captcha.ts 的预解池设计：
- 热路径永不等待：请求到来时直接从池里取一枚已解好的 token（亚毫秒），
  后台任务持续补充库存（目标 min，上限 max）。
- token 时效：verifyParam 实际 TTL ~2 分钟，池内按 FIFO + 年龄淘汰，
  超过 token_ttl 的直接丢弃重解。
- 挑战失效：上游返回挑战时 invalidate() 清空整池（该批指纹可能已被
  风控盯上，继续复用只会连环 3007）。
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from . import constants, egress, logs, settings
from .store import store

# 池参数：真浏览器求解重（10–40s/枚），默认 min1/max2 见 settings 注释
POOL_MIN = settings.CAPTCHA_POOL_MIN
POOL_MAX = settings.CAPTCHA_POOL_MAX
TOKEN_TTL_MS = settings.CAPTCHA_TOKEN_TTL  # 单枚 token 的最大可用时长（ms）


def _now_ms() -> float:
    """墙上时间（ms）。配置缓存用，不能换成 monotonic（重启后要与 TTL 比对）。"""
    return time.time() * 1000


class CaptchaSolveError(Exception):
    """验证码求解最终失败（重试耗尽/求解器不可用）。

    独立于 ClaimError 体系（验证码先于领域层使用），路由层按兜底回执处理。
    """


class _Token:
    __slots__ = ("param", "region", "born_at", "egress")

    def __init__(self, param: str, region: str | None, egress_tag: str = egress.DIRECT) -> None:
        self.param = param
        self.region = region
        self.born_at = time.monotonic()
        self.egress = egress_tag

    def expired(self) -> bool:
        return (time.monotonic() - self.born_at) * 1000 >= TOKEN_TTL_MS


class CaptchaManager:
    def __init__(self) -> None:
        # 按出口分池：token 只能用在签发它的那个出口上，混池会让请求与
        # token 的 IP 对不上，上游判 unusual activity（见 app/egress.py）。
        self._pools: dict[str, asyncio.Queue[_Token]] = {}
        self._pool_sizes: dict[str, int] = {}
        self._refill_task: asyncio.Task | None = None
        self._refilling = False
        self._config_lock = asyncio.Lock()
        # 配置按出口缓存：上游按出口 IP 给不同的 captcha 配置（实测直连 region=cn、
        # 走 US 代理 region=sgp），混用会把错地区的 token 配到请求上。
        self._config_cache: dict[str, dict] = {}
        self._config_cache_at: dict[str, float] = {}
        self._last_error: str | None = None
        # 热路径触发的补货任务强引用（事件循环只持弱引用，裸 create_task 会被 GC）
        self._bg_tasks: set[asyncio.Task] = set()

    # ── 出口 ─────────────────────────────────────────────────────────────────
    def hot_egress(self) -> str:
        """预热哪个出口的池：默认跟随 messages 上游的实际出口。

        只有 messages 是热路径（每次请求都要验证码）；claim 一小时一轮，
        按需现解即可，不必占第二个池的内存。
        """
        forced = settings.CAPTCHA_POOL_EGRESS
        return forced or egress.egress_for(settings.UPSTREAM["zai"])

    def _pool_for(self, tag: str) -> asyncio.Queue[_Token]:
        pool = self._pools.get(tag)
        if pool is None:
            pool = asyncio.Queue(maxsize=POOL_MAX)
            self._pools[tag] = pool
            self._pool_sizes[tag] = 0
        return pool

    def _size(self, tag: str) -> int:
        return self._pool_sizes.get(tag, 0)

    # ── 配置 ─────────────────────────────────────────────────────────────────
    async def fetch_config(self, egress_tag: str | None = None) -> dict:
        """取 captcha 配置（按出口缓存）。`egress_tag` 决定拉取走哪条出口。"""
        tag = egress_tag or self.hot_egress()
        ttl = settings.CAPTCHA_CONFIG_CACHE_TTL
        if self._config_cache.get(tag) and _now_ms() - self._config_cache_at.get(tag, 0) < ttl:
            return self._config_cache[tag]
        async with self._config_lock:
            # 双检：等锁期间可能已被其他请求填充
            if self._config_cache.get(tag) and _now_ms() - self._config_cache_at.get(tag, 0) < ttl:
                return self._config_cache[tag]
            err: Exception | None = None
            for _ in range(2):  # 15s 走 US 代理偶发 ReadTimeout，重试一次（err 的 str 常为空）
                try:
                    async with httpx.AsyncClient(
                        timeout=30, trust_env=tag == egress.PROXY
                    ) as client:
                        res = await client.get(
                            f"{constants.CLIENT_CONFIGS_URL}?{constants.CLIENT_CONFIGS_QUERY}"
                        )
                    res.raise_for_status()
                    captcha = ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
                    if captcha:
                        self._config_cache[tag] = captcha
                        self._config_cache_at[tag] = _now_ms()
                        return captcha
                except (httpx.HTTPError, ValueError) as exc:
                    err = exc
            logs.warn(
                "captcha",
                f"获取配置失败（出口={tag}），使用默认: {type(err).__name__ if err else '无响应'}: {err}",
            )
            return dict(constants.CAPTCHA_DEFAULTS)

    async def model_requests_need_captcha(self, egress_tag: str | None = None) -> bool:
        """模型请求是否该带验证码——官方 `requiresRequestVerification` 的等价物。

        bundle 原文：`mode==="start-plan" && enabled!==false && skipModelRequest!==true`。
        线上配置 `skip_model_request:true` ⇒ 官方客户端在模型请求上**不带**
        `X-Aliyun-Captcha-Verify-Param`；hub 长期无条件带，这正是「官方不会干的
        事」，也是 3012 的结构性嫌疑。无配置时官方仍要求验证码，故默认 False。
        """
        config = await self.fetch_config(egress_tag)
        return config.get("enabled") is not False and config.get("skip_model_request") is not True

    # ── 预解池 ───────────────────────────────────────────────────────────────
    def start(self) -> None:
        """启动后台补充循环（main.py lifespan 调用）。"""
        if self._refill_task is None or self._refill_task.done():
            self._refill_task = asyncio.create_task(self._refill_loop())

    async def close(self) -> None:
        if self._refill_task and not self._refill_task.done():
            self._refill_task.cancel()
            try:
                await self._refill_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._refill_task = None

    def _gate_open(self) -> bool:
        """是否允许预热：存在可走 Plan 对话的 jwt 账号才预热。

        apiKey 回退 / 废 JWT / 风控禁用 / 额度用完 / 冷却 都不需要验证码。
        账号冷却状态随 store 落库，重启后全冷却期间此门保持关闭。
        """
        return any(
            a.allows_billing() and a.is_selectable()
            for a in store.list_accounts("zai")
        )

    async def _refill_loop(self) -> None:
        while True:
            try:
                tag = self.hot_egress()
                # 无可服务账号（全冷却/禁用/无号）：只淘汰过期 token，不解新码（不产生上游流量）
                if not self._gate_open():
                    await self._evict_expired(tag)
                    await asyncio.sleep(3)
                    continue
                # 上游对模型请求免验证码（skip_model_request）时池无人取用，
                # 继续预热只是白烧阿里云求解流量与 chrome 内存
                if not await self.model_requests_need_captcha(tag):
                    await self._evict_expired(tag)
                    await asyncio.sleep(30)
                    continue
                need = POOL_MIN - self._size(tag)
                if need > 0:
                    await self._refill_batch(need, tag)
                else:
                    await self._evict_expired(tag)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - 后台循环永不退出
                self._last_error = str(err)
                logs.warn("captcha", f"补充循环异常: {err}")
                await asyncio.sleep(5)

    async def _refill_batch(self, need: int, tag: str | None = None) -> None:
        """串行补充（求解有 CPU 开销，避免并发爆 Node 进程）。"""
        if self._refilling:
            return
        self._refilling = True
        tag = tag or self.hot_egress()
        try:
            config = await self.fetch_config(tag)
            for _ in range(need):
                if self._size(tag) >= POOL_MAX:
                    break
                token = await self._solve_one(config, tag)
                if token is None:
                    break
                self._put(token, tag)
        finally:
            self._refilling = False

    def _put(self, token: _Token, tag: str | None = None) -> None:
        tag = tag or token.egress
        try:
            self._pool_for(tag).put_nowait(token)
            self._pool_sizes[tag] = self._size(tag) + 1
        except asyncio.QueueFull:
            pass

    async def _evict_expired(self, tag: str | None = None) -> None:
        tags = [tag] if tag else list(self._pools)
        for one in tags:
            pool = self._pools.get(one)
            if pool is None:
                continue
            kept: list[_Token] = []
            while True:
                try:
                    token = pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._pool_sizes[one] = max(0, self._size(one) - 1)
                if not token.expired() and len(kept) < POOL_MAX:
                    kept.append(token)
            for token in kept:
                self._put(token, one)

    async def get_verify_param(self, egress_tag: str | None = None) -> tuple[str, str | None]:
        """取一枚可用 token：优先池内现成的（跳过过期），池空才同步现解。

        `egress_tag` 必须是本次请求真实使用的出口（`proxy` / `direct`），
        否则拿到的 token 与请求 IP 不一致，上游会判 unusual activity。
        只有热路径出口有池；其它出口现解。

        返回 (verify_param, region)。region 可为 None（旧求解器无 region 概念）。
        """
        tag = egress_tag or self.hot_egress()
        pooled = tag == self.hot_egress()
        pool = self._pool_for(tag)

        # 1) 池内直取（仅热路径出口有池）
        while pooled and self._size(tag) > 0:
            try:
                token = pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pool_sizes[tag] = max(0, self._size(tag) - 1)
            if token.egress == tag and not token.expired():
                # 触发后台补货（fire-and-forget；_refilling 防重入，强引用防 GC）
                task = asyncio.create_task(self._refill_batch(1, tag))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
                return token.param, token.region

        # 2) 池空/全过期/非热路径出口：同步现解一次
        config = await self.fetch_config(tag)
        token = await self._solve_one(config, tag)
        if token is None:
            raise CaptchaSolveError(f"验证码求解失败: {self._last_error or '多次重试无结果'}")
        return token.param, token.region

    # ── 求解 ─────────────────────────────────────────────────────────────────
    async def _solve_one(self, config: dict, tag: str = egress.DIRECT) -> _Token | None:
        scene = config.get("sceneId") or constants.CAPTCHA_DEFAULTS["sceneId"]
        region = config.get("region") or constants.CAPTCHA_DEFAULTS["region"]
        prefix = config.get("prefix") or constants.CAPTCHA_DEFAULTS["prefix"]

        last_err: str | None = None
        for attempt in range(1, settings.CAPTCHA_SOLVE_RETRIES + 1):
            try:
                param = await self._run_solver(scene, region, prefix, tag)
            except Exception as err:  # noqa: BLE001
                last_err = str(err)
                param = None
            if param:
                if attempt > 1:
                    logs.ok("captcha", f"求解成功（第 {attempt} 次尝试，出口={tag}）")
                return _Token(param, region, tag)
            self._last_error = last_err
            logs.warn("captcha", f"第 {attempt}/{settings.CAPTCHA_SOLVE_RETRIES} 次求解未果，重试…")

        logs.warn("captcha", f"求解失败: {last_err or '多次重试无结果'}")
        return None

    async def _run_solver(
        self, scene: str, region: str, prefix: str, tag: str = egress.DIRECT
    ) -> str | None:
        solver = settings.CAPTCHA_SOLVER_JS
        if not solver.exists():
            raise RuntimeError(
                f"未找到求解器 {solver}，请先在 captcha_node 下执行 npm install"
            )
        # 出口必须与取用该 token 的请求一致（见 app/egress.py）：
        #   direct → 剥掉代理变量，chrome 不带 --proxy-server
        #   proxy  → 注入代理变量，solver 用它作 --proxy-server
        solver_env = {**os.environ, "ZCODE_CHROMIUM_PATH": str(settings.CHROMIUM_PATH)}
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                    "ALL_PROXY", "all_proxy"):
            solver_env.pop(key, None)
        if tag == egress.PROXY:
            proxy = egress.proxy_url()
            if not proxy:
                raise RuntimeError("请求走代理但 env 未配置代理地址，无法对齐验证码出口")
            solver_env["HTTP_PROXY"] = proxy
            solver_env["HTTPS_PROXY"] = proxy
        proc = await asyncio.create_subprocess_exec(
            settings.NODE_PATH, str(solver), scene, region, prefix,
            cwd=str(settings.CAPTCHA_SOLVER_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=solver_env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.CAPTCHA_SOLVE_TIMEOUT)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None
        except FileNotFoundError as err:
            raise RuntimeError(f"无法启动 Node（{settings.NODE_PATH}）: {err}") from err

        param = None
        for line in stdout.decode("utf-8", "ignore").splitlines():
            if line.startswith("VERIFY_PARAM="):
                param = line[len("VERIFY_PARAM="):].strip()
        return param

    # ── 失效 ─────────────────────────────────────────────────────────────────
    def invalidate(self, tag: str | None = None) -> None:
        """上游返回验证码挑战时清空该出口的池（该批 token/指纹已不可信）。

        默认只清热路径出口：只按出口清，避免把另一条通道（如 claim 的直连池）
        一起废掉。传 `tag=None` 且需要全清时用 `invalidate_all()`。
        """
        self._drain(tag or self.hot_egress())

    def invalidate_all(self) -> None:
        for tag in list(self._pools):
            self._drain(tag)

    def _drain(self, tag: str) -> None:
        pool = self._pools.get(tag)
        if pool is None:
            return
        drained = 0
        while True:
            try:
                pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pool_sizes[tag] = max(0, self._size(tag) - 1)
            drained += 1
        if drained:
            logs.warn("captcha", f"验证码失效（{tag} 出口），清空池 {drained} 枚")


captcha_manager = CaptchaManager()
