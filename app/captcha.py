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
import signal
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
    活着的 node，PPID≠1，不会误杀）。

    判据用 puppeteer 的临时 profile 目录（`--user-data-dir=<tmp>/puppeteer_dev_chrome_profile-*`），
    而不是裸 `chromium` 子串——后者会把同机其它 Chromium/Chrome 服务一起 SIGKILL。
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
            if "puppeteer_dev_chrome_profile" not in cmdline:
                continue
            os.kill(int(entry), signal.SIGKILL)
            killed += 1
        except (OSError, ValueError, IndexError):
            continue
    return killed


def _cleanup_stale_profiles(max_age_seconds: int = 3600) -> int:
    """清掉 puppeteer 留下的旧临时 profile 目录（2.7.33）。

    Chromium 被 SIGKILL 后 `--user-data-dir=/tmp/puppeteer_dev_chrome_profile-*`
    不会被删，长期运行会堆满 /tmp（单个目录几十 MB）。只删 mtime 超过
    max_age 的——并发求解中活跃浏览器的 profile 一直在写，不会被误删。
    """
    import glob
    import shutil
    import tempfile

    removed = 0
    cutoff = time.time() - max_age_seconds
    pattern = os.path.join(tempfile.gettempdir(), "puppeteer_dev_chrome_profile-*")
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


class CaptchaManager:
    def __init__(self) -> None:
        # 按出口分池：token 只能用在签发它的那个出口上，混池会让请求与
        # token 的 IP 对不上，上游判 unusual activity（见 app/egress.py）。
        self._pools: dict[str, asyncio.Queue[_Token]] = {}
        self._pool_sizes: dict[str, int] = {}
        self._refill_task: asyncio.Task | None = None
        self._refilling = False
        self._config_lock = asyncio.Lock()
        # 求解并发上限（进程级）：池空突发时不按请求数拉起 N 个 Chromium
        self._solve_sem = asyncio.Semaphore(settings.CAPTCHA_MAX_CONCURRENT_SOLVES)
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
        async with self._config_lock:
            # 双检：等锁期间可能已被其他请求填充
            if self._config_cache.get(tag) and _now_ms() - self._config_cache_at.get(tag, 0) < ttl:
                return self._config_cache[tag]
            err: Exception | None = None

            async def _try_fetch(proxy_url: str | None) -> dict | None:
                """单次拉取；显式 proxy 优先，否则按 tag 走 trust_env 旧语义。"""
                try:
                    if proxy_url:
                        client_cm = httpx.AsyncClient(timeout=30, proxy=proxy_url,
                                                      trust_env=False)
                    else:
                        client_cm = httpx.AsyncClient(timeout=30,
                                                      trust_env=tag == egress.PROXY)
                    async with client_cm as client:
                        res = await client.get(
                            f"{constants.CLIENT_CONFIGS_URL}?{constants.CLIENT_CONFIGS_QUERY}"
                        )
                    res.raise_for_status()
                    return ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
                except (httpx.HTTPError, ValueError) as exc:
                    nonlocal err
                    err = exc
                    return None

            def _legacy_primary_proxy() -> str | None:
                return egress.proxy_url() if tag == egress.PROXY else None

            captcha: dict | None = None
            for _ in range(2):  # 15s 走 US 代理偶发 ReadTimeout，同出口重试一次
                captcha = await _try_fetch(proxy if proxy is not None else _legacy_primary_proxy())
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

    async def get_verify_param(self, egress_tag: str | None = None,
                               proxy: str | None = None) -> tuple[str, str | None]:
        """取一枚可用 token：优先池内现成的（跳过过期），池空才同步现解。

        `egress_tag` 必须是本次请求真实使用的出口（per-account 模式 = 账号绑定
        代理 URL），否则拿到的 token 与请求 IP 不一致，上游会判 unusual
        activity。只有热路径出口有池；其它出口现解。

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
        token = await self._solve_one(config, tag, proxy)
        if token is None:
            raise CaptchaSolveError(f"验证码求解失败: {self._last_error or '多次重试无结果'}")
        return token.param, token.region

    # ── 求解 ─────────────────────────────────────────────────────────────────
    async def _solve_one(self, config: dict, tag: str = egress.DIRECT,
                         proxy: str | None = None) -> _Token | None:
        scene = config.get("sceneId") or constants.CAPTCHA_DEFAULTS["sceneId"]
        region = config.get("region") or constants.CAPTCHA_DEFAULTS["region"]
        prefix = config.get("prefix") or constants.CAPTCHA_DEFAULTS["prefix"]

        last_err: str | None = None
        for attempt in range(1, settings.CAPTCHA_SOLVE_RETRIES + 1):
            try:
                # 进程级并发上限：超出排队等前一个浏览器退出，避免内存被打满
                async with self._solve_sem:
                    param = await self._run_solver(scene, region, prefix, tag, proxy)
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
        self, scene: str, region: str, prefix: str, tag: str = egress.DIRECT,
        proxy: str | None = None,
    ) -> str | None:
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
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.CAPTCHA_SOLVE_TIMEOUT)
        except TimeoutError:
            return None
        except FileNotFoundError as err:
            raise RuntimeError(f"无法启动 Node（{settings.NODE_PATH}）: {err}") from err
        finally:
            # 整组 SIGKILL 兜底：node 挂死（实测 NAS 上可挂 25 分钟以上）、launch 失败
            # 残留、browser.close 异常被吞等路径都会留下 Chromium 孤儿（单个 ~360MB，
            # 此前实测 10 分钟泄漏数 GB），必须由调用侧无条件清场。
            # setsid 后 pgid == proc.pid；node 已正常退出且浏览器已关闭时组为空，
            # 抛 ProcessLookupError 属正常情况，无副作用。
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (AttributeError, ProcessLookupError, PermissionError):
                # Windows 无 killpg/SIGKILL（AttributeError）：退化为杀 node 本体，
                # 否则超时路径会留下常驻 node 进程
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:  # noqa: BLE001 - 含等待超时；CancelledError 不在此列，正常传播
                pass
            # 二道防线：Chromium 逃逸了 node 进程组（setsid），killpg 打不到，按孤儿
            # 特征（PPID==1）兜底清扫。必须放在 node 确认死亡之后：SIGKILL 投递是异步的，
            # 早扫会因孤儿 PPID 尚未 reparent 到 1 而漏过（实测场景复现）
            orphans = _kill_orphan_chromium()
            if orphans:
                logs.warn("captcha", f"清理孤儿 Chromium 进程 {orphans} 个")
            # 被 SIGKILL 的浏览器留下的临时 profile 目录没人删：每次求解顺手清
            # 一遍陈旧目录（>1h），避免 /tmp 长期膨胀（2.7.33）
            stale = _cleanup_stale_profiles()
            if stale:
                logs.info("captcha", f"清理陈旧浏览器 profile 目录 {stale} 个")

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
