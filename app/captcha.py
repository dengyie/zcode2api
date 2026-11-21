"""验证码求解 + 预解 token 池。

主求解器 slidex_runner.py（缺省，2026-11-16）：playwright + slidex
`aliyun-captcha-js` provider（z.ai 站点实测内化的求解内核：二次几何反解 /
拟人拖拽 / F 码归因）运行阿里云官方无痕 SDK 求 verifyParam。根修 Chrome
`--proxy-server` 不支持带凭证代理 URL（ERR_NO_SUPPORTED_PROXIES）的求解
盲区——playwright context 级 proxy 原生支持 user:pass@，token 出口 =
请求出口铁律不变。

solver_pw.js（puppeteer 路线）保留作回滚：ZCODE_CAPTCHA_SOLVER=pw。
阿里云无痕验证信任真实浏览器环境，happy-dom 伪造路线 2026-09 起被风控
「unusual activity」全拒（solver.js 仅作 ZCODE_CAPTCHA_SOLVER=legacy 回滚）。

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
import signal
import sys
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


def _kill_orphan_chromium() -> int:
    """清扫孤儿 Chromium 进程（PPID==1），返回击杀数。

    实测：Chromium 启动后会 setsid 脱离 node 的进程组（PGID=SESS=自身），
    node 正常退出时由 solver 侧 exit 钩子清理；但 node 被 SIGKILL（求解超时）
    或同步卡死时，killpg(node 组) 打不到逃逸的 Chromium，必须按孤儿特征兜底：
    父进程已死的 Chromium 必为泄漏残留（并发求解中活跃 Chromium 的父进程是
    活着的 node/driver，PPID≠1，不会误杀）。

    判据用两条求解链的临时 profile 目录特征（`--user-data-dir=<tmp>/…`），
    而不是裸 `chromium` 子串——后者会把同机其它 Chromium/Chrome 服务一起
    SIGKILL：
    - pw/legacy 路线（puppeteer）：`puppeteer_dev_chrome_profile-*`
    - slidex 路线（playwright，缺省）：`playwright_chromiumdev_profile-*`
      （2026-11-18 review P2#7 补——slidex 自 2.7.49 起为主求解器，其逃逸
      Chromium 一直不被二道防线识别；命名经 tebi 实测确认）
    非 Linux（无 /proc）直接跳过。
    """
    killed = 0
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return 0
    for entry in entries:
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                raw = f.read().decode("utf-8", "ignore")
            # stat 格式：pid (comm) state ppid ...（comm 可含空格/括号，从右起找最后的 ")"）
            ppid = int(raw.rsplit(")", 1)[1].split()[1])
            if ppid != 1:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", "ignore")
            if "puppeteer_dev_chrome_profile" not in cmdline and \
                    "playwright_chromiumdev_profile" not in cmdline:
                continue
            os.kill(int(entry), signal.SIGKILL)
            killed += 1
        except (OSError, ValueError, IndexError):
            continue
    return killed


def _cleanup_stale_profiles(max_age_seconds: int = 3600) -> int:
    """清掉求解器留下的旧临时 profile 目录（2.7.33；2026-11-18 补 playwright 特征）。

    Chromium 被 SIGKILL 后 `--user-data-dir=<tmp>/…` 不会被删，长期运行会堆满
    /tmp（单个目录几十 MB）。只删 mtime 超过 max_age 的——并发求解中活跃
    浏览器的 profile 一直在写，不会被误删。两条求解链的命名特征都收：
    puppeteer（pw/legacy 路线）+ playwright（slidex 缺省路线，tebi 实测命名）。
    """
    import glob
    import shutil
    import tempfile

    removed = 0
    cutoff = time.time() - max_age_seconds
    patterns = [
        os.path.join(tempfile.gettempdir(), "puppeteer_dev_chrome_profile-*"),
        os.path.join(tempfile.gettempdir(), "playwright_chromiumdev_profile-*"),
    ]
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                if os.path.getmtime(path) > cutoff:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            except OSError:
                continue
    return removed


class _Token:
    __slots__ = ("param", "region", "born_at", "egress")

    def __init__(self, param: str, region: str | None, egress_tag: str = egress.DIRECT) -> None:
        self.param = param
        self.region = region
        self.born_at = time.monotonic()
        self.egress = egress_tag

    def expired(self) -> bool:
        return (time.monotonic() - self.born_at) * 1000 >= TOKEN_TTL_MS


def _normalize_captcha_region(config: dict, tag: str) -> None:
    """sgp 归一（2.7.59，配置边界单点）。

    上游按出口 IP 下发 region（US TP 出口实测 sgp、直连 cn），但 sgp 端点
    headless 无痕稳定必败（2026-11-17 对照实验：同出口同窗口 cn 一跳成功、
    sgp 6/6 TIMEOUT/F001）——两代求解器均已把 sgp 归一为 SDK 缺省 cn 端点
    出参（solver_pw.js 2026-11-11、slidex_runner 2.7.58）。参数签发端点是
    cn，`X-Aliyun-Captcha-Verify-Region` 头也必须是 cn：头与签发端点错位
    = 上游验签必败 3007（2.7.58 当天实弹 4 号连败「求解成功→claim 3007」
    的全链根因——_Token.region 存的是「传给 solver 的值」而非「solver
    实际签发用的值」，语义缺环）。在配置边界归一，签发/校验头/手动领取
    （admin captcha-config 前端 SDK）三个消费者看到同一个 region。
    """
    if (config.get("region") or "").strip().lower() == "sgp":
        config["region"] = "cn"
        logs.info("captcha", f"出口 {tag} 配置 region=sgp 归一为 cn（sgp 端点 headless 必败，签发与校验头同走 cn）")


def _timeout_attribution(attribution: str | None) -> bool:
    """超时型归因判定（2.7.68 链式超时止损）。

    覆盖 11-18 晚主故障形态：DEADLINE（runner 硬截止）/ TRACELESS:TIMEOUT /
    EXC:TimeoutError / SETUP:TimeoutError / 求解进程超时（>240s SIGKILL）。
    这些形态共享同一语义——求解链没能在预算内完成——换 context / 换求解进程
    都改变不了环境，连续出现即环境级失败（与 DEGRADED 同级），继续跑满重试
    预算纯白烧（当晚 159 次求解 0 成功，主形态即链式超时，每号白烧 16 分钟）。
    """
    if not attribution:
        return False
    up = attribution.upper()
    return "TIMEOUT" in up or "DEADLINE" in up or "求解进程超时" in attribution


class CaptchaManager:
    def __init__(self) -> None:
        # 按出口分池：token 只能用在签发它的那个出口上，混池会让请求与
        # token 的 IP 对不上，上游判 unusual activity（见 app/egress.py）。
        self._pools: dict[str, asyncio.Queue[_Token]] = {}
        self._pool_sizes: dict[str, int] = {}
        self._refill_task: asyncio.Task | None = None
        self._refilling = False
        # 配置拉取锁按出口分键（2026-11-21 review P3）：全局单锁下，A 出口的
        # 15s 网络拉取会阻塞 B 出口的缓存命中路径（锁内含 httpx I/O）；分键后
        # 各出口互不牵连。锁随 TTL 失效自动淘汰，防历史 tag 累积。
        self._config_locks: dict[str, asyncio.Lock] = {}
        # 求解并发上限（进程级）：池空突发时不按请求数拉起 N 个 Chromium
        self._solve_sem = asyncio.Semaphore(settings.CAPTCHA_MAX_CONCURRENT_SOLVES)
        # 配置按出口缓存：上游按出口 IP 给不同的 captcha 配置（实测直连 region=cn、
        # 走 US 代理 region=sgp），混用会把错地区的 token 配到请求上。
        self._config_cache: dict[str, dict] = {}
        self._config_cache_at: dict[str, float] = {}
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
    async def fetch_config(self, egress_tag: str | None = None,
                           proxy: str | None = None) -> dict:
        """取 captcha 配置（按出口缓存）。

        `egress_tag`/`proxy` 决定拉取走哪条出口——per-account 出口隔离下二者
        都是账号绑定代理 URL；region 由上游按拉取 IP 下发，缓存按 tag 隔离即
        按 IP 隔离（混用会把错地区的 token 配到请求上）。`proxy` 非 None 时
        不做跨出口兜底（换出口拉的配置对不上账号 IP）。
        """
        tag = egress_tag or self.hot_egress()
        ttl = settings.CAPTCHA_CONFIG_CACHE_TTL
        if self._config_cache.get(tag) and _now_ms() - self._config_cache_at.get(tag, 0) < ttl:
            return self._config_cache[tag]
        # 按出口分键锁（双检模式不变）；锁上限裁剪防历史 tag 无界增长
        if len(self._config_locks) > 64 and tag not in self._config_locks:
            self._config_locks.clear()
        config_lock = self._config_locks.setdefault(tag, asyncio.Lock())
        async with config_lock:
            # 双检：等锁期间可能已被其他请求填充
            if self._config_cache.get(tag) and _now_ms() - self._config_cache_at.get(tag, 0) < ttl:
                return self._config_cache[tag]
            err: Exception | None = None

            async def _try_fetch(use_proxy: bool) -> dict | None:
                """单次拉取：per-account 显式代理优先（trust_env=False，与 env
                语义解耦）；legacy 模式按 use_proxy 走 trust_env（尊重
                NO_PROXY，与 2.7.35 前行为一致）。"""
                try:
                    if proxy:
                        client_cm = httpx.AsyncClient(timeout=30, proxy=proxy,
                                                      trust_env=False)
                    else:
                        client_cm = httpx.AsyncClient(timeout=30, trust_env=use_proxy)
                    async with client_cm as client:
                        res = await client.get(
                            # billing 链版本锚定（内联 f-string 读当前值，
                            # constants.py 注释：模块级快照会随 version_tracker
                            # 运行期重赋而过期）
                            f"{constants.CLIENT_CONFIGS_URL}"
                            f"?app_version={constants.BILLING_APP_VERSION}",
                            # 官方桌面端形态 UA：缺省时 httpx 默认 UA 自报
                            # python-httpx 身份，与 install 同端点路径（带
                            # ZCode/<ver> UA）形状分裂（2026-11-21 claim 链
                            # 一致性迭代）。
                            headers={"User-Agent": f"ZCode/{constants.BILLING_APP_VERSION}"},
                        )
                    res.raise_for_status()
                    return ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
                except (httpx.HTTPError, ValueError) as exc:
                    nonlocal err
                    err = exc
                    return None

            captcha: dict | None = None
            for _ in range(2):  # 15s 走 US 代理偶发 ReadTimeout，同出口重试一次
                captcha = await _try_fetch(tag == egress.PROXY)
                if captcha:
                    break
            if not captcha and proxy is None:
                # 主出口两连败 → 另一条出口兜底（2026-11-04 线上实锤：proxy 整晚
                # 超时而 direct 稳定；配置拿不到会误开「模型请求带码」→ 3012）。
                # per-account 模式不做跨出口兜底——换出口拉的配置对不上账号 IP。
                alt = egress.DIRECT if tag == egress.PROXY else egress.PROXY
                captcha = await _try_fetch(alt == egress.PROXY)
                if captcha:
                    logs.info("captcha", f"出口 {tag} 拉取失败，已用 {alt} 兜底")
            if captcha:
                _normalize_captcha_region(captcha, tag)
                self._config_cache[tag] = captcha
                self._config_cache_at[tag] = _now_ms()
                return captcha
            # 两条出口都失败：优先沿用该出口上一次成功拉到的配置（stale-while-
            # error）。退回 CAPTCHA_DEFAULTS 会让 model_requests_need_captcha()
            # 认为「该带码」，而实际配置可能是 skip_model_request=true —— 给
            # 官方不会带码的请求带上验证码头 = 3012 的结构性嫌疑（2.7.33）。
            stale = self._config_cache.get(tag)
            if stale:
                logs.warn(
                    "captcha",
                    f"获取配置失败（出口={tag}，含兜底），沿用上次缓存配置"
                    f"（{type(err).__name__ if err else '无响应'}: {err}）",
                )
                return stale
            logs.warn(
                "captcha",
                f"获取配置失败（出口={tag}，含兜底），使用默认: "
                f"{type(err).__name__ if err else '无响应'}: {err}",
            )
            return dict(constants.CAPTCHA_DEFAULTS)

    async def model_requests_need_captcha(self, egress_tag: str | None = None,
                                          proxy: str | None = None) -> bool:
        """模型请求是否该带验证码——官方 `requiresRequestVerification` 的等价物。

        bundle 原文：`mode==="start-plan" && enabled!==false && skipModelRequest!==true`。
        线上配置 `skip_model_request:true` ⇒ 官方客户端在模型请求上**不带**
        `X-Aliyun-Captcha-Verify-Param`；hub 长期无条件带，这正是「官方不会干的
        事」，也是 3012 的结构性嫌疑。无配置时官方仍要求验证码，故默认 False。
        """
        config = await self.fetch_config(egress_tag, proxy)
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
        # 热路径触发的补货任务一并收口（2026-11-21 review P3）：原先只收
        # _refill_task，池空触发的 _refill_batch 在停机后仍持引用逃逸
        bg = list(self._bg_tasks)
        self._bg_tasks.clear()
        for t in bg:
            if not t.done():
                t.cancel()
        if bg:
            await asyncio.gather(*bg, return_exceptions=True)

    def _gate_open(self) -> bool:
        """是否允许**预热**：存在可走 Plan 对话的 jwt 账号才预热。

        预热本身就是上游流量，因此只跟着「能服务对话」的号走：apiKey 回退 /
        废 JWT / 风控禁用 / 额度用完 / 冷却 都不预热，重启后全冷却期间保持关门。
        注意这只管预热——领取（billing 链）不受此门约束：冷却/停放号要领取时，
        `get_verify_param` 池空会现解（不依赖预热池，见 2026-11-04 两条链口径）。
        """
        return any(
            a.uses_plan_channel() and a.is_selectable()
            for a in store.list_accounts("zai")
        )

    async def _refill_loop(self) -> None:
        warned_per_account = False
        while True:
            try:
                tag = self.hot_egress()
                # per-account 出口隔离（2.7.36）：预热带 tag 的 token 永远不会
                # 被账号取用（账号 tag = 各自绑定代理 URL）——按关闭处理，防白烧
                # 求解流量与 Chromium 内存。部署约束：per-account 模式下预热恒关。
                if settings.PER_ACCOUNT_EGRESS:
                    if not warned_per_account:
                        logs.info("captcha", "per-account 出口模式：验证码预热停用（按账号现解）")
                        warned_per_account = True
                    await self._evict_expired(tag)
                    await asyncio.sleep(30)
                    continue
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
                token, _err = await self._solve_one(config, tag)
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

    async def get_verify_param(self, egress_tag: str | None = None,
                               proxy: str | None = None,
                               retries: int | None = None) -> tuple[str, str | None]:
        """取一枚可用 token：优先池内现成的（跳过过期），池空才同步现解。

        `egress_tag` 必须是本次请求真实使用的出口（per-account 模式 = 账号绑定
        代理 URL），否则拿到的 token 与请求 IP 不一致，上游会判 unusual
        activity。只有热路径出口有池；其它出口现解。
        `retries`：本调用的求解重试预算覆盖（2.7.68 预算解耦）——claim 链传
        `CAPTCHA_CLAIM_SOLVE_RETRIES`（缺省 2），messages 池路径不传走全量
        `CAPTCHA_SOLVE_RETRIES`（热路径可用性兜底值得跑满）。

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
        config = await self.fetch_config(tag, proxy)
        token, err = await self._solve_one(config, tag, proxy, max_retries=retries)
        if token is None:
            raise CaptchaSolveError(f"验证码求解失败: {err or '多次重试无结果'}")
        return token.param, token.region

    # ── 求解 ─────────────────────────────────────────────────────────────────
    async def _solve_one(self, config: dict, tag: str = egress.DIRECT,
                         proxy: str | None = None,
                         max_retries: int | None = None,
                         ) -> tuple[_Token | None, str | None]:
        """跑满重试预算求一枚 token。返回 (token | None, 最终归因 | None)。

        归因（2026-11-18 review P2#6 根修）从共享单槽 `self._last_error` 改为
        本调用局部变量：_solve_sem 只包住 _run_solver 进程段，此前的共享槽在
        多账号并发求解（admin /claim 逐号 + 轮次 + 消息热路径池空现解）下互相
        覆盖，A 号的 DEGRADED 止损判定可能读到 B 号的异常文案——误止损/漏止损。
        `max_retries`：重试预算覆盖（2.7.68 预算解耦）——claim 链传小值防单号
        长尾吃穿领取窗口；None = settings.CAPTCHA_SOLVE_RETRIES 全量。
        """
        scene = config.get("sceneId") or constants.CAPTCHA_DEFAULTS["sceneId"]
        region = config.get("region") or constants.CAPTCHA_DEFAULTS["region"]
        prefix = config.get("prefix") or constants.CAPTCHA_DEFAULTS["prefix"]
        # 显式 0 = 「本轮禁用重试」（调用方契约），不能被 `or` 吞成默认值——
        # claim 链传小值防长尾，0 意为 1 次尝试即止（2026-11-21 review P3）
        retries = settings.CAPTCHA_SOLVE_RETRIES if max_retries is None \
            else max(1, int(max_retries))

        attribution: str | None = None
        last_err: str | None = None
        timeout_streak = 0  # 连续超时型归因计数（跨 attempt，非超时归因清零）
        for attempt in range(1, retries + 1):
            fail_code: str | None = None
            try:
                # 进程级并发上限：超出排队等前一个浏览器退出，避免内存被打满
                async with self._solve_sem:
                    param, fail_code = await self._run_solver(scene, region, prefix, tag, proxy)
            except Exception as err:  # noqa: BLE001
                last_err = str(err)
                param = None
            if param:
                if attempt > 1:
                    logs.ok("captcha", f"求解成功（第 {attempt} 次尝试，出口={tag}）")
                return _Token(param, region, tag), None
            # 归因优先级：本轮 fail_code（求解器内部归因，如 SLIDEX_FAIL /
            # 求解超时）> 异常文案 > 往轮归因（延续）。共享槽竞态已随局部化根除。
            attribution = fail_code or last_err or attribution
            logs.warn("captcha", f"第 {attempt}/{retries} 次求解未果，重试…")
            # 环境级失败快速止损（2.7.60 实弹定案）：DEGRADED = 阿里云对
            # headless 无痕的判定收紧（2026-11-18 深夜对照实验：TP 出口与
            # 直连 CN 同窗全灭，同形态「假成功回调降级 param + popup 不出」
            # ——与出口 IP 无关）。换 context / 换求解进程都无法改变环境
            # 判定，4 连重试 × 240s 预算纯白烧（每号 16 分钟）+ 反复暴露
            # 求解行为指纹。归因为 DEGRADED 时直接放弃本轮后续重试。
            if "DEGRADED" in (attribution or ""):
                logs.warn(
                    "captcha",
                    f"求解归因 DEGRADED（headless 判定收紧，环境级失败）——止损，"
                    f"不再重试（出口={tag}）",
                )
                break
            # 链式超时止损（2.7.68）：DEADLINE / TIMEOUT / 求解进程超时等形态
            # 此前不命中任何止损，每号照样跑满 4×240s。连续
            # CAPTCHA_TIMEOUT_GIVEUP_AFTER 次（换过 context/进程仍超时）=
            # 环境级失败，第 N 次起不再白烧。
            if _timeout_attribution(attribution):
                timeout_streak += 1
                if timeout_streak >= settings.CAPTCHA_TIMEOUT_GIVEUP_AFTER:
                    logs.warn(
                        "captcha",
                        f"求解归因连续 {timeout_streak} 次超时型"
                        f"（{attribution}）——环境级失败，止损不再重试（出口={tag}）",
                    )
                    break
            else:
                timeout_streak = 0

        msg = attribution or last_err or "多次重试无结果"
        logs.warn("captcha", f"求解失败: {msg}")
        return None, msg

    async def _run_solver(
        self, scene: str, region: str, prefix: str, tag: str = egress.DIRECT,
        proxy: str | None = None,
    ) -> tuple[str | None, str | None]:
        """求解一次。返回 (param | None, fail_code | None)——fail_code 为求解器
        内部归因（SLIDEX_FAIL 码 / 求解超时），异常仍向上抛。"""
        if settings.CAPTCHA_SOLVER_MODE == "slidex":
            return await self._run_solver_slidex(scene, region, prefix, tag, proxy)
        return await self._run_solver_pw(scene, region, prefix, tag, proxy)

    async def _run_solver_slidex(
        self, scene: str, region: str, prefix: str, tag: str,
        proxy: str | None,
    ) -> tuple[str | None, str | None]:
        """slidex 路线：python runner + playwright context 级 credentialed proxy。

        出口对齐口径与 _run_solver_pw 完全一致：proxy 参数优先，其次按 tag 取
        env 代理；tag=proxy 但无代理可用必须响亮失败（静默求解 = 出口错配 =
        3012）。进程清场（killpg/孤儿/陈旧 profile）复用 pw 路径同款 finally 链。
        """
        if not settings.CAPTCHA_SOLVER_PY.exists():
            raise RuntimeError(f"未找到求解器 {settings.CAPTCHA_SOLVER_PY}")
        solver_env = {
            **os.environ,
            "ZCODE_CHROMIUM_PATH": str(settings.CHROMIUM_PATH),
            "ZCODE_SOLVE_DEADLINE_MS": str(max(30, settings.CAPTCHA_SOLVE_TIMEOUT - 20) * 1000),
        }
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                    "ALL_PROXY", "all_proxy"):
            solver_env.pop(key, None)
        solver_proxy = proxy if proxy is not None else (
            egress.proxy_url() if tag == egress.PROXY else None)
        if solver_proxy:
            solver_env["HTTP_PROXY"] = solver_proxy
            solver_env["HTTPS_PROXY"] = solver_proxy
        elif tag == egress.PROXY:
            raise RuntimeError("请求走代理但 env 未配置代理地址，无法对齐验证码出口")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(settings.CAPTCHA_SOLVER_PY), scene, region, prefix,
            cwd=str(settings.CAPTCHA_SOLVER_DIR),
            stdout=asyncio.subprocess.PIPE,
            # stderr 收管（2.7.58）：slidex 的 CAPTCHA_DEBUG 诊断（fatal 异常
            # /F0xx 归因）全走 stderr，DEVNULL 吞掉 = 失败潮时零归因线索
            # （2026-11-17 领取连败排查实证：只能盲猜出口抖动）。
            stderr=asyncio.subprocess.PIPE,
            env=solver_env,
            start_new_session=True,
        )
        try:
            stdout, stderr = await self._communicate_solver(proc)
        except TimeoutError:
            # 240s SIGKILL：runner 的 SLIDEX_FAIL/stderr 全丢（2026-11-18 review
            # P2#5——今晚「多次重试无结果」的精确源头：首拍超时即全程盲归因）
            return None, (
                f"求解进程超时（>{settings.CAPTCHA_SOLVE_TIMEOUT}s 被 SIGKILL，"
                f"SLIDEX_FAIL/stderr 全丢，归因不可得）"
            )
        finally:
            await self._cleanup_solver(proc)

        param = None
        fail_code = None
        for line in stdout.decode("utf-8", "ignore").splitlines():
            if line.startswith("VERIFY_PARAM="):
                param = line[len("VERIFY_PARAM="):].strip()
            elif line.startswith("SLIDEX_FAIL="):
                # 诊断归因行（F001=阿里云行为风控全拒/ARM_SHAPE/DRAG/…）：
                # 正常退出但无 param 的路径 _solve_one 靠它归因，
                # 否则失败文案只有笼统的「多次重试无结果」（2.7.50 review P3
                # ——实弹 briarlow17 四轮全盲归因的教训）
                fail_code = line[len("SLIDEX_FAIL="):].strip() or None
        if param is None and fail_code:
            fail_code = f"slidex 求解未果（{fail_code}）"
        if param is None:
            # stderr 尾部诊断透传（fatal: <异常> / attempt 归因）——只取
            # 尾部 3 行防刷屏，成功路径完全不产生日志
            tail = [
                ln for ln in stderr.decode("utf-8", "ignore").splitlines()
                if ln.strip()
            ][-3:]
            if tail:
                logs.warn("captcha", f"slidex stderr: {' | '.join(tail)}")
        return param, fail_code

    async def _run_solver_pw(
        self, scene: str, region: str, prefix: str, tag: str = egress.DIRECT,
        proxy: str | None = None,
    ) -> tuple[str | None, str | None]:
        solver = settings.CAPTCHA_SOLVER_JS
        if not solver.exists():
            raise RuntimeError(
                f"未找到求解器 {solver}，请先在 captcha_node 下执行 npm install"
            )
        # 出口必须与取用该 token 的请求一致（见 app/egress.py）：
        #   direct → 剥掉代理变量，chrome 不带 --proxy-server
        #   proxy  → 注入代理变量，solver 用它作 --proxy-server
        # per-account 模式（2.7.36）：proxy = 账号绑定代理 URL，每账号独立出口
        solver_env = {
            **os.environ,
            "ZCODE_CHROMIUM_PATH": str(settings.CHROMIUM_PATH),
            # solver 侧硬超时：早于本侧 SIGKILL 20s 自我了断并清场，
            # 避免"被 SIGKILL 打断 → 逃逸 Chromium 无人清理"
            "ZCODE_SOLVE_DEADLINE_MS": str(max(30, settings.CAPTCHA_SOLVE_TIMEOUT - 20) * 1000),
        }
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                    "ALL_PROXY", "all_proxy"):
            solver_env.pop(key, None)
        solver_proxy = proxy if proxy is not None else (
            egress.proxy_url() if tag == egress.PROXY else None)
        if solver_proxy:
            solver_env["HTTP_PROXY"] = solver_proxy
            solver_env["HTTPS_PROXY"] = solver_proxy
        elif tag == egress.PROXY:
            # legacy 共享模式：tag=proxy 但 env 无代理 → 无法对齐验证码出口，
            # 必须响亮失败（静默求解 = token 与请求 IP 不一致 = 3012）
            raise RuntimeError("请求走代理但 env 未配置代理地址，无法对齐验证码出口")
        proc = await asyncio.create_subprocess_exec(
            settings.NODE_PATH, str(solver), scene, region, prefix,
            cwd=str(settings.CAPTCHA_SOLVER_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=solver_env,
            start_new_session=True,  # 独立进程组：收尾时整组清场（见下方 finally）
        )
        try:
            stdout, _ = await self._communicate_solver(proc)
        except TimeoutError:
            return None, f"求解进程超时（>{settings.CAPTCHA_SOLVE_TIMEOUT}s 被 SIGKILL，归因不可得）"
        except FileNotFoundError as err:
            raise RuntimeError(f"无法启动 Node（{settings.NODE_PATH}）: {err}") from err
        finally:
            await self._cleanup_solver(proc)

        param = None
        for line in stdout.decode("utf-8", "ignore").splitlines():
            if line.startswith("VERIFY_PARAM="):
                param = line[len("VERIFY_PARAM="):].strip()
        return param, None

    async def _communicate_solver(self, proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        return await asyncio.wait_for(proc.communicate(), timeout=settings.CAPTCHA_SOLVE_TIMEOUT)

    async def _cleanup_solver(self, proc: asyncio.subprocess.Process) -> None:
        """求解进程无条件清场（slidex/pw 两路线共用）。

        整组 SIGKILL 兜底：求解器挂死（实测 NAS 上可挂 25 分钟以上）、launch 失败
        残留、browser.close 异常被吞等路径都会留下 Chromium 孤儿（单个 ~360MB，
        此前实测 10 分钟泄漏数 GB），必须由调用侧无条件清场。
        setsid 后 pgid == proc.pid；求解器已正常退出且浏览器已关闭时组为空，
        抛 ProcessLookupError 属正常情况，无副作用。
        """
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError):
            # Windows 无 killpg/SIGKILL（AttributeError）：退化为杀主进程本体，
            # 否则超时路径会留下常驻求解进程
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:  # noqa: BLE001 - 含等待超时；CancelledError 不在此列，正常传播
            pass
        # 二道防线：Chromium 逃逸了解决器进程组（setsid），killpg 打不到，按孤儿
        # 特征（PPID==1）兜底清扫。必须放在求解器确认死亡之后：SIGKILL 投递是异步的，
        # 早扫会因孤儿 PPID 尚未 reparent 到 1 而漏过（实测场景复现）
        orphans = _kill_orphan_chromium()
        if orphans:
            logs.warn("captcha", f"清理孤儿 Chromium 进程 {orphans} 个")
        # 被 SIGKILL 的浏览器留下的临时 profile 目录没人删：每次求解顺手清
        # 一遍陈旧目录（>1h），避免 /tmp 长期膨胀（2.7.33）
        stale = _cleanup_stale_profiles()
        if stale:
            logs.info("captcha", f"清理陈旧浏览器 profile 目录 {stale} 个")

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
