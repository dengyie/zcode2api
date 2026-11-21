"""FastAPI 应用工厂 + 生命周期。"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import logs, plan_guard, reqlog, settings
from .captcha import captcha_manager
from .claim import claim_round
from .egress_pool import egress_pool  # 单例实例（module.start 不存在——start 是实例方法）
from .quota import monitor
from .routes import admin_api, gateway, pages
from .store import store
from .version_tracker import version_tracker


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles with `Cache-Control: no-cache` — etag revalidation on every
    request so deploys are visible immediately (CF/browser never serve stale)."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# 修正 Windows 中文控制台可能出现的乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _display_host() -> str:
    # 0.0.0.0 / 空地址在浏览器中不可直接访问，展示为 127.0.0.1
    host = (settings.HOST or "").strip()
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def _backfill_fingerprints() -> list:
    """启动时给无指纹或非桌面 SKU（旧版宿主机克隆）的存量账号换发生成档案。

    换机后清 installed_at，由 lifespan 补跑按账号安装序（新 device_mid 需要
    自己的 app_launch / app_daily_active）。返回被替换的账号列表。
    """
    from .fingerprint import DeviceProfile, is_generated_sku, profile_for
    from .store import store

    replaced = []
    for account in store.list_accounts():
        fp = account.fingerprint
        needs = False
        if not isinstance(fp, dict) or not fp.get("device_mid"):
            needs = True
        else:
            try:
                profile = DeviceProfile(
                    platform=fp["platform"], arch=fp["arch"], os_version=fp["os_version"],
                    language=fp["language"], timezone=fp["timezone"], screen=fp["screen"],
                    device_mid=fp["device_mid"],
                )
            except (KeyError, TypeError, ValueError):
                needs = True
            else:
                needs = not is_generated_sku(profile)
        if needs:
            account.fingerprint = None
            account.installed_at = None
            profile_for(account)
            store._assign_fingerprint(account)
            store.update_account(account)
            replaced.append(account)
    return replaced


def _backfill_install_ids() -> int:
    """启动时给无 install_id 的存量账号补配安装身份（纯本地，无网络）并落库。"""
    import uuid as _uuid

    from .store import store

    backfilled = 0
    for account in store.list_accounts():
        if not (account.install_id or "").strip():
            account.install_id = str(_uuid.uuid4())
            store.update_account(account)
            backfilled += 1
    return backfilled


# 启动安装序的后台任务引用：事件循环对 task 只持弱引用（asyncio 官方文档），
# 不保存引用任务可能被 GC 中途丢弃且无日志 —— 与 captcha._refill_task 同一模式
_install_task: asyncio.Task | None = None


def _startup_precheck() -> None:
    """发版偏斜自检（2.7.68）：发版 = scp 真身，偏斜窗口内「新 main.py + 旧
    egress_pool.py」曾以运行期 AttributeError 崩溃（err.log 实锤，supervisor
    autorestart 拉起后自愈）——把这类不匹配从运行期 AttributeError 提前为
    启动期一条明确版本配对错误，fail-fast 带完整上下文。"""
    missing = [name for name, obj, *attrs in (
        ("egress_pool", egress_pool, "start", "stop"),
        ("captcha_manager", captcha_manager, "start", "close"),
        ("claim_round", claim_round, "start", "stop"),
        ("version_tracker", version_tracker, "start", "stop"),
        ("monitor", monitor, "start", "stop"),
    ) if not all(hasattr(obj, a) for a in attrs)]
    if missing:
        raise RuntimeError(
            f"发版偏斜：缺少 {', '.join(missing)} 的生命周期方法 —— app/ 真身不完整"
            f"或版本不配对，请重新完整发版（不要只 scp 单文件）")


def _start_component(name: str, fn) -> None:
    """单组件启动隔离（2.7.68）：任一后台组件 start 抛异常只降级该组件并留痕，
    不再放大成整个 lifespan 失败 → 进程崩溃循环 + HA 看门狗连环重启（在途
    请求全砍）。"""
    try:
        fn()
    except Exception as err:  # noqa: BLE001 - 单组件失败不拖垮启动
        logs.err("lifespan",
                 f"{name} 启动失败（降级运行，其余组件不受影响）: "
                 f"{type(err).__name__}: {err}")


def _run_install_sequence_on_start() -> None:
    """启动后台执行一次安装序（官方客户端每次启动都拉 configs + 发 app_launch，
    日活去重在上游按 device_mid+日期）。失败只留痕，绝不影响启动。"""
    global _install_task

    from . import install

    async def _run() -> None:
        try:
            await install.run_install_sequence()
        except Exception as err:  # noqa: BLE001 —— 后台任务异常无人接收，必须自兜
            logs.err("install", f"安装序意外异常: {err}")

    _install_task = asyncio.create_task(_run())


@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_precheck()
    # 启动数据面容错（2026-11-22 review P1 根修）：restore/backfill/warm 是
    # 同步关键段且处理存量落盘数据——reqlog.jsonl 坏行、单条账号畸形字段等
    # 任意非预期异常原先直接炸 lifespan → supervisor autorestart 崩溃循环，
    # 与 _start_component 的「单组件失败降级运行」哲学自相矛盾（越靠前的启动
    # 代码越无保护）。数据面失败降级跳过该步并留痕，启动继续；_startup_precheck
    # 的版本配对 fail-fast 语义不变（那是发版偏斜守卫，非数据面）。
    try:
        if settings.REQLOG_PERSIST:
            restored = reqlog.restore()
            if restored:
                logs.ok("reqlog", f"请求监控日志恢复 ×{restored}（持久化开启）")
    except Exception as err:  # noqa: BLE001 - 数据面降级，不阻断启动
        logs.err("lifespan", f"reqlog.restore 失败（跳过恢复，服务继续）: "
                             f"{type(err).__name__}: {err}")
    try:
        replaced = _backfill_fingerprints()
        if replaced:
            logs.ok("fingerprint", f"存量账号补配独立设备指纹 ×{len(replaced)}")
            for acc in replaced:
                admin_api._schedule_install(acc)
    except Exception as err:  # noqa: BLE001
        logs.err("lifespan", f"指纹回填失败（跳过，服务继续）: "
                             f"{type(err).__name__}: {err}")
    try:
        installed = _backfill_install_ids()
        if installed:
            logs.ok("install", f"存量账号补配安装身份 ×{installed}")
    except Exception as err:  # noqa: BLE001
        logs.err("lifespan", f"安装身份回填失败（跳过，服务继续）: "
                             f"{type(err).__name__}: {err}")
    try:
        plan_guard.warm()  # 冷却表回读+过期行清理前移到启动，调度路径不承担磁盘 I/O
    except Exception as err:  # noqa: BLE001
        logs.err("lifespan", f"plan_guard 预热失败（跳过，服务继续）: "
                             f"{type(err).__name__}: {err}")
    # env 钉死告警（2.7.68 review P3）：admin_key 首启写库后 env 轮换静默失效
    # ——「以为换了锁其实没换」。启动时对账一条 warn。
    try:
        if os.getenv("ZCODE_ADMIN_KEY") and store.admin_key() != settings.DEFAULT_ADMIN_KEY:
            logs.warn("lifespan",
                      "env ZCODE_ADMIN_KEY 与数据库 admin_key 不一致，以数据库为准"
                      "（env 仅首次启动写库生效）；如需轮换请在后台设置页修改")
    except Exception:  # noqa: BLE001 - 纯观测告警，不阻断启动
        pass
    _start_component("quota monitor", monitor.start)
    _start_component("captcha pool", captcha_manager.start)
    _start_component("claim round", claim_round.start)
    _start_component("version tracker", version_tracker.init_from_store)
    _start_component("version tracker", version_tracker.start)
    _start_component("egress pool", egress_pool.start)
    _run_install_sequence_on_start()
    base = f"http://{_display_host()}:{settings.PORT}"
    logs.banner([
        f"{logs._B}{logs._MAG}zcode-hub{logs._R} {logs._DIM}v{settings.APP_VERSION} · Python{logs._R}",
        f"{logs._DIM}后台管理{logs._R}  {logs._C}{base}/admin/login{logs._R}",
        f"{logs._DIM}对话端点{logs._R}  {logs._C}{base}/v1/messages{logs._R}",
    ])
    try:
        yield
    finally:
        # fire-and-forget 后台任务集收口（2026-11-21 review P2）：领取/安装/
        # 登录跟单任务可能在「上游已扣减、本地未落库」的窗口被硬砍。先给在途
        # 任务一个有限宽限窗口自然完成，超时才 cancel——领取结果不丢。
        import asyncio as _aio

        from . import install

        pending: set = set()
        pending.update(admin_api._auto_claim_tasks)
        pending.update(admin_api._login_followup_tasks)
        pending.update(admin_api._claim_job_tasks)
        pending.update(install._install_tasks)
        # gateway fire-and-forget 任务收编（2026-11-22 review P2 根修）：原先
        # 只收 admin/install，gateway._bg_tasks（verify_then_rebalance 等，内含
        # 分钟级 rebalance）漏出清单——停机硬砍落在「TP 已 force_rebalance、
        # hub 未持久化」窗口，记录分叉要等 1800s reconcile 才自愈。
        pending.update(gateway._bg_tasks)
        if _install_task is not None and not _install_task.done():
            pending.add(_install_task)
        if pending:
            done, still = await _aio.wait(pending, timeout=10.0)
            for t in still:
                t.cancel()
            if still:
                await _aio.gather(*still, return_exceptions=True)
            logs.info("lifespan", f"后台任务收口: {len(done)} 完成, {len(still)} 取消")
        # 停机逐段隔离（2.7.68）：首段抛异常会跳过其余收口（在途请求证据丢失、
        # 后台任务逃逸）——每段独立 try/except，停机路径绝不中断
        for name, fn in (
            ("reqlog", lambda: reqlog.shutdown_flush()),
            ("opslog", lambda: logs.opslog_close()),
            ("monitor", monitor.stop),
            ("claim_round", claim_round.stop),
            ("version_tracker", version_tracker.stop),
            ("egress_pool", egress_pool.stop),
            ("captcha_manager", captcha_manager.close),
        ):
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
                if name == "reqlog" and result:
                    logs.info("reqlog", f"停机收口在途请求 ×{result}")
            except asyncio.CancelledError:
                # 收口协程自身被 cancel 不能放弃剩余组件（review P3：原先
                # 直接 raise 会让第一段之后的全部收口跳过）；吞掉并继续，
                # 宽限窗口已过，此刻的 cancel 是 uvicorn 关循环的最后一刀
                continue
            except Exception as err:  # noqa: BLE001 - 停机收口绝不互相牵连
                logs.err("lifespan", f"{name} 停机收口异常（已跳过）: "
                                     f"{type(err).__name__}: {err}")


def create_app() -> FastAPI:
    app = FastAPI(title="zcode-hub", version=settings.APP_VERSION, lifespan=lifespan)

    # 前后端分离：前端默认同源 /static 服务，设置 ZCODE_CORS_ORIGINS 后即可
    # 部署到任意源跨域调用本 API（Bearer 鉴权，无 cookie，CSRF 面为零）。
    origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=86400,
        )



    app.include_router(pages.router)
    app.include_router(admin_api.router)
    app.include_router(gateway.router)
    app.mount("/static", RevalidatingStaticFiles(directory=str(settings.FRONTEND_DIR)), name="static")
    # 前端相对资产路径（js/... css/...）在 /admin/* 页面形态下的服务点；路由
    # （/admin/api/*、/admin/login 等）注册在前，优先级高于本挂载。
    # JS/CSS 等静态资产必须 no-cache（发版后 CF/浏览器立即 revalidate 新内容）：
    # StaticFiles 默认无 Cache-Control 头，CF heuristic 缓存会把旧 JS 边缘缓存数小时。
    app.mount("/admin", RevalidatingStaticFiles(directory=str(settings.FRONTEND_DIR / "admin"), html=True),
              name="admin-static")
    return app


app = create_app()
