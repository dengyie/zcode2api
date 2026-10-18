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

from . import constants, logs, settings
from .store import store

# 池参数：真浏览器求解重（10–40s/枚），默认 min1/max2 见 settings 注释
POOL_MIN = settings.CAPTCHA_POOL_MIN
POOL_MAX = settings.CAPTCHA_POOL_MAX
TOKEN_TTL_MS = settings.CAPTCHA_TOKEN_TTL  # 单枚 token 的最大可用时长（ms）


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
    """
    killed = 0
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return 0  # 非 Linux（本地开发）无此机制
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
            if "chromium" not in cmdline and "chrome_" not in cmdline:
                continue
            os.kill(int(entry), signal.SIGKILL)
            killed += 1
        except (OSError, ValueError, IndexError):
            continue
    return killed


class _Token:
    __slots__ = ("param", "region", "born_at")

    def __init__(self, param: str, region: str | None) -> None:
        self.param = param
        self.region = region
        self.born_at = time.monotonic()

    def expired(self) -> bool:
        return (time.monotonic() - self.born_at) * 1000 >= TOKEN_TTL_MS


class CaptchaManager:
    def __init__(self) -> None:
        self._pool: asyncio.Queue[_Token] = asyncio.Queue(maxsize=POOL_MAX)
        self._pool_size = 0          # Queue 无可信 len，自行维护
        self._refill_task: asyncio.Task | None = None
        self._refilling = False
        self._config_lock = asyncio.Lock()
        self._config_cache: dict | None = None
        self._config_cache_at: float = 0.0
        self._last_error: str | None = None
        # 热路径触发的补货任务强引用（事件循环只持弱引用，裸 create_task 会被 GC）
        self._bg_tasks: set[asyncio.Task] = set()

    # ── 配置 ─────────────────────────────────────────────────────────────────
    async def fetch_config(self) -> dict:
        now = time.time() * 1000
        if self._config_cache and now - self._config_cache_at < settings.CAPTCHA_CONFIG_CACHE_TTL:
            return self._config_cache
        async with self._config_lock:
            # 双检：等锁期间可能已被其他请求填充
            if self._config_cache and time.time() * 1000 - self._config_cache_at < settings.CAPTCHA_CONFIG_CACHE_TTL:
                return self._config_cache
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    res = await client.get(
                        f"{constants.CLIENT_CONFIGS_URL}?{constants.CLIENT_CONFIGS_QUERY}"
                    )
                res.raise_for_status()
                captcha = ((res.json().get("data") or {}).get("configs") or {}).get("captcha")
                if captcha:
                    self._config_cache = captcha
                    self._config_cache_at = time.time() * 1000
                    return captcha
            except (httpx.HTTPError, ValueError) as err:
                logs.warn("captcha", f"获取配置失败，使用默认: {err}")
            return dict(constants.CAPTCHA_DEFAULTS)

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
                # 无可服务账号（全冷却/禁用/无号）：只淘汰过期 token，不解新码（不产生上游流量）
                if not self._gate_open():
                    await self._evict_expired()
                    await asyncio.sleep(3)
                    continue
                need = POOL_MIN - self._pool_size
                if need > 0:
                    await self._refill_batch(need)
                else:
                    await self._evict_expired()
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - 后台循环永不退出
                self._last_error = str(err)
                logs.warn("captcha", f"补充循环异常: {err}")
                await asyncio.sleep(5)

    async def _refill_batch(self, need: int) -> None:
        """串行补充（求解有 CPU 开销，避免并发爆 Node 进程）。"""
        if self._refilling:
            return
        self._refilling = True
        try:
            config = await self.fetch_config()
            for _ in range(need):
                if self._pool_size >= POOL_MAX:
                    break
                token = await self._solve_one(config)
                if token is None:
                    break
                self._put(token)
        finally:
            self._refilling = False

    def _put(self, token: _Token) -> None:
        try:
            self._pool.put_nowait(token)
            self._pool_size += 1
        except asyncio.QueueFull:
            pass

    async def _evict_expired(self) -> None:
        kept: list[_Token] = []
        while True:
            try:
                token = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pool_size = max(0, self._pool_size - 1)
            if not token.expired() and len(kept) < POOL_MAX:
                kept.append(token)
        for token in kept:
            self._put(token)

    async def get_verify_param(self, port: int | None = None) -> tuple[str, str | None]:
        """取一枚可用 token：优先池内现成的（跳过过期），池空才同步现解。

        返回 (verify_param, region)。region 可为 None（旧求解器无 region 概念）。
        """
        # 1) 池内直取
        while self._pool_size > 0:
            try:
                token = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pool_size = max(0, self._pool_size - 1)
            if not token.expired():
                # 触发后台补货（fire-and-forget；_refilling 防重入，强引用防 GC）
                task = asyncio.create_task(self._refill_batch(1))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
                return token.param, token.region

        # 2) 池空/全过期：同步现解一次（首启兜底；正常情况下后台循环已预热）
        config = await self.fetch_config()
        token = await self._solve_one(config)
        if token is None:
            raise CaptchaSolveError(f"验证码求解失败: {self._last_error or '多次重试无结果'}")
        return token.param, token.region

    # ── 求解 ─────────────────────────────────────────────────────────────────
    async def _solve_one(self, config: dict) -> _Token | None:
        scene = config.get("sceneId") or constants.CAPTCHA_DEFAULTS["sceneId"]
        region = config.get("region") or constants.CAPTCHA_DEFAULTS["region"]
        prefix = config.get("prefix") or constants.CAPTCHA_DEFAULTS["prefix"]

        last_err: str | None = None
        for attempt in range(1, settings.CAPTCHA_SOLVE_RETRIES + 1):
            try:
                param = await self._run_solver(scene, region, prefix)
            except Exception as err:  # noqa: BLE001
                last_err = str(err)
                param = None
            if param:
                if attempt > 1:
                    logs.ok("captcha", f"求解成功（第 {attempt} 次尝试）")
                return _Token(param, region)
            self._last_error = last_err
            logs.warn("captcha", f"第 {attempt}/{settings.CAPTCHA_SOLVE_RETRIES} 次求解未果，重试…")

        logs.warn("captcha", f"求解失败: {last_err or '多次重试无结果'}")
        return None

    async def _run_solver(self, scene: str, region: str, prefix: str) -> str | None:
        solver = settings.CAPTCHA_SOLVER_JS
        if not solver.exists():
            raise RuntimeError(
                f"未找到求解器 {solver}，请先在 captcha_node 下执行 npm install"
            )
        # 强制直连求解：token 求解出口必须与 billing/claim 一致（claim 已改直连，
        # 见 _billing_request 注释）；剥离代理变量后 chrome 不带 --proxy-server，
        # 出口确定，不受 hub env 的 US 代理影响。
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
            except (ProcessLookupError, PermissionError):
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

        param = None
        for line in stdout.decode("utf-8", "ignore").splitlines():
            if line.startswith("VERIFY_PARAM="):
                param = line[len("VERIFY_PARAM="):].strip()
        return param

    # ── 失效 ─────────────────────────────────────────────────────────────────
    def invalidate(self) -> None:
        """上游返回验证码挑战时清空整池（该批 token/指纹已不可信）。"""
        drained = 0
        while True:
            try:
                self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._pool_size = max(0, self._pool_size - 1)
            drained += 1
        if drained:
            logs.warn("captcha", f"验证码失效，清空池 {drained} 枚")


captcha_manager = CaptchaManager()
