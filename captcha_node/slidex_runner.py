#!/usr/bin/env python3
"""slidex aliyun-captcha-js solver runner（zcode-hub claim 链专用）。

用法（与 solver_pw.js 同契约）：
    python3 slidex_runner.py <sceneId> <region> <prefix>

输出契约（stdout，被 app/captcha.py._run_solver 解析）：
    VERIFY_PARAM=<captchaVerifyParam>   成功
    SLIDEX_FAIL=<fail_code>             失败（诊断用，不影响退出码）

根修背景（2026-11-16 活动套餐领券事故）：Chrome `--proxy-server` 不支持带凭证
代理 URL（ERR_NO_SUPPORTED_PROXIES）→ AliyunCaptcha.js SDK 加载失败 → 求解
20s 超时。本 runner 改用 playwright context 级 proxy（HTTP CONNECT，原生支持
username/password），出口对齐铁律不变：token 出口 = claim 请求出口。

求解内核复用 slidex `aliyun-captcha-js` provider（z.ai 站点实测内化：
二次几何反解 / 双检测器 / 拟人拖拽 / F 码归因），不重造轮子。
环境变量：
    ZCODE_CHROMIUM_PATH   浏览器可执行路径（缺省 /usr/local/bin/chromium）
    ZCODE_SOLVE_DEADLINE_MS  整体预算 ms（超时不再开新尝试）
    HTTP_PROXY/HTTPS_PROXY   带凭证代理 URL（http://user:pass@host:port）
    SLIDEX_DRAG           plan（缺省，GestureSession 拟人手势家族轮换 + CDP
                          可信事件流水线，slidex 0.13.3 plan_factory）|
                          real（内置三段匀速，回滚用）| synthetic（页面内合成
                          事件，isTrusted=false 诊断用）
    CAPTCHA_DEBUG=1       日志走 stderr（stdout 只出契约行）
"""
from __future__ import annotations

import asyncio
import os
import sys
import urllib.parse
from pathlib import Path

_PAGE = Path(__file__).resolve().parent / "page.html"


def _verify_param_ok(param: str | None) -> bool:
    """严格校验（对齐老 solver.js extractVerifyParam / zapi 口径）。

    真实 verifyParam ~280 字符 base64-JSON，内含 securityToken（≥50 字符）。
    len-76 的降级结果（无 securityToken）上游必然 3007——2026-11-17 实弹
    链「求解成功→claim 3007」的另一半根因：降级产物被当成功输出，重试
    白烧后 claim 必败。降级结果绝不当成功放行，交由外层换 context 重试。
    """
    if not param or len(param) < 200:
        return False
    try:
        import base64
        import json as _json
        pad = param + "=" * (-len(param) % 4)
        decoded = _json.loads(base64.b64decode(pad))
        sec = decoded.get("securityToken") or decoded.get("SecurityToken")
        return bool(sec) and len(str(sec)) >= 50
    except Exception:  # noqa: BLE001 — 形态不对即降级
        return False


async def _guarded(coro, timeout_s: float, fail_code: str) -> dict:
    """相位级硬截止：超时返回合成失败结果，绝不让异常穿出 attempt 循环。"""
    try:
        return await asyncio.wait_for(coro, timeout=max(6.0, timeout_s))
    except asyncio.TimeoutError:
        return {"param": None, "verdict": None, "fail_code": fail_code}


def _debug(msg: str) -> None:
    if os.environ.get("CAPTCHA_DEBUG", "").lower() in ("1", "true", "yes"):
        print(f"[slidex] {msg}", file=sys.stderr, flush=True)


def _playwright_proxy() -> dict | None:
    """带凭证代理 URL → playwright context proxy（HTTP CONNECT）。"""
    raw = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()
    if not raw:
        return None
    u = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
    if not u.hostname or not u.port:
        raise ValueError(f"bad proxy URL: {u.hostname=!r} {u.port=!r}")
    out: dict = {"server": f"http://{u.hostname}:{u.port}"}
    if u.username:
        out["username"] = urllib.parse.unquote(u.username)
    if u.password:
        out["password"] = urllib.parse.unquote(u.password)
    return out


async def _solve(scene: str, region: str, prefix: str) -> int:
    from playwright.async_api import async_playwright
    from slidex.providers.aliyun_captcha_js import AliyunCaptchaJsProvider

    proxy = _playwright_proxy()
    exe = os.environ.get("ZCODE_CHROMIUM_PATH") or "/usr/local/bin/chromium"
    deadline_ms = int(os.environ.get("ZCODE_SOLVE_DEADLINE_MS") or "220000")
    drag = os.environ.get("SLIDEX_DRAG", "plan").strip().lower()
    if drag == "rotate":
        # 0.13.3：旧 rotate（synthetic/real 按 attempt 交替）被手势家族轮换
        # 取代——家族轮换在 GestureSession 内部完成，且 synthetic 全灭
        # （isTrusted=false → F024），交替白烧一半 attempt。
        drag = "plan"
    _debug(f"scene={scene} region={region} prefix={prefix} proxy={'yes' if proxy else 'no'} "
           f"deadline={deadline_ms}ms drag={drag} exe={exe}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path=exe if Path(exe).exists() else None,
            # 指纹加固（2026-11-18 实弹探针）：缺省 navigator.webdriver=true
            # 是显式机器人标记。摘不掉无头本质，但先摘最便宜的那顶帽子。
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            # 硬预算兜底（2026-11-18 20:32 实弹）：attempt 内部的相位没有自我
            # 截止时，app 侧 240s SIGKILL 会连 SLIDEX_FAIL/stderr 一起吞掉，
            # 归因全丢（日志只剩「归因不可得」）。这里用外层 wait_for 保证
            # 契约行一定落地：超时 → SLIDEX_FAIL=DEADLINE。
            return await asyncio.wait_for(
                _attempt_in_browser(
                    pw, browser, AliyunCaptchaJsProvider,
                    scene, region, prefix, proxy, drag, deadline_ms,
                ),
                timeout=deadline_ms / 1000.0 + 8,
            )
        except asyncio.TimeoutError:
            print("SLIDEX_FAIL=DEADLINE", flush=True)
            _debug("hard deadline hit outside attempt loop")
            return 1
        finally:
            await browser.close()


async def _attempt_in_browser(
    pw, browser, provider_cls, scene, region, prefix, proxy, drag, deadline_ms
) -> int:
    import time as _time

    start = _time.monotonic()
    # 0.13.3 接线：GestureSession 由 runner 持有（solver.py 同源规划器：一次
    # solve 只建一个 session，attempt 级 next() + 反馈码）。attempt 间换全新
    # context（F001 换新会话 doctrine）不变；家族轮换/重采样语义由 session
    # 内部完成——F001/F015 回执换家族，无回执失败同家族重采样。
    from slidex._gestures import PACKET_MISS_CODE, GestureSession
    from slidex._slide_result import UPSTREAM_REJECT_CODE

    session = GestureSession(None, "zcode-claim", "auto")
    feedback = {"code": None}

    def _plan_factory(target_px: float, _prev_verdict):
        # 本 runner 每次 acquire 只发 1 个 attempt，provider 传来的
        # prev_verdict 恒 None；跨 attempt 的反馈码由 feedback 携带。
        plan = session.next(float(target_px), feedback["code"])
        feedback["code"] = None
        if plan is None:
            _debug("gesture session exhausted (no plan)")
        else:
            _debug(f"plan={plan.archetype} steps={len(plan.points)} "
                   f"overshoot={plan.extra_overshoot}")
        return plan

    # F001/F015 归因重试（slidex 手册规范）：行为风控拒绝后换全新 context
    # （新指纹/新控件会话）再试，直至预算耗尽。
    last_fail = None
    attempt = 0  # 预算 ≤0 时循环不进，末尾 debug 引用不得 NameError
    while _time.monotonic() - start < deadline_ms / 1000.0:
        attempt += 1
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            proxy=proxy,
            # 指纹加固（2026-11-18 实弹探针）：缺省 UA = HeadlessChrome/149
            # （自带无头招牌），languages=['en-US'] 与 TZ=Asia/Shanghai 矛盾。
            # UA 对齐真实 Chrome（平台保持 Linux，与 navigator.platform 一致）
            # + 中文 locale。
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        try:
            page = await context.new_page()
            # setup 段（goto + SDK 就绪）异常不得逃出 attempt 循环——2026-11-18
            # 19:1x 实弹：慢 alicdn 节点（trial 24.7s/整文件）上 45s 等不到
            # initAliyunCaptcha，TimeoutError 直接穿到 main() 顶层兜底，归因
            # 退化成裸 EXC:TimeoutError，claim 侧只见「求解未果」丢失环境线索。
            try:
                await page.goto(_PAGE.as_uri(), wait_until="load", timeout=30_000)
                # SDK 就绪（与 solver_pw.js 同款等待；SDK 从 o.alicdn.com 拉取，
                # 经 context proxy 出站——此处失败=出口不通，fast-fail 归因）。
                # 预算 45s（2026-11-18 1117 复盘）：TP 节点对 alicdn 路由实测
                # 1.7~31.9s/资源，20s 连可用档（3.5~5s）的多资源串行都接不住。
                await page.wait_for_function(
                    "typeof window.initAliyunCaptcha === 'function'", timeout=45_000
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 环境级失败：归因后换 context
                last_fail = f"SETUP:{type(exc).__name__}"
                _debug(f"attempt {attempt}: setup failed: {last_fail}; retrying")
                continue
            # 第一跳：无痕验证（solver_pw.js 线上实证稳定形态）——成功直接出
            # param，不需要拼图。失败（SDK 弹 popup 兜底）再落 slidex 拟人拖拽。
            # 单相位硬预算（2026-11-18 20:32 实弹）：traceless 在现风控态下
            # 恒 TIMEOUT/F001（19:00 窗口全量证据），45s 纯浪费且吃光整轮
            # 预算，导致拖拽 ladder 一次都跑不到就被 SIGKILL。
            # traceless 可跳过（SLIDEX_TRACELESS=0）：现风控态下无痕恒 TIMEOUT，
            # 且超时态下 SDK 后端请求仍挂着，随后 provider ARM 必然 popup not
            # visible（2026-11-18 20:51 实弹：5 连 attempt 全 DRAG:ARM）。直落
            # popup 滑块的 provider 路径才能拿到真实 F 码/param。
            _skip_tl = os.environ.get("SLIDEX_TRACELESS", "1").strip().lower() in (
                "0", "false", "no", "off")
            try:
                traceless = "SKIP" if _skip_tl else await asyncio.wait_for(page.evaluate(
                """(args) => new Promise((resolve) => {
                    let done = false;
                    const finish = (v) => { if (!done) { done = true; resolve(v); } };
                    try {
                        // region 归一（2.7.58 + 2.7.66 修正）：上游按出口 IP 下发
                        // sgp（美国 TP 出口拉配置即得），但 sgp 端点 traceless 稳定
                        // TIMEOUT/F001（2026-11-17 对照实验：同出口同窗口 cn 一次
                        // 成功、sgp 6 轮全败）——故统一钉 cn 端点。
                        // 2.7.66 实证：cn/sgp 也不能省 AliyunCaptchaConfig——SDK
                        // 缺省不再回落 CN 端点，直接请求
                        // https://undefined.captcha-open.aliyuncs.com/（requestfailed
                        // + 无痕假成功回调吐 len-76 降级 param，claim 必 3007）。
                        window.AliyunCaptchaConfig = { region: "cn", prefix: args.prefix };
                        window.__slidexCaptchaParam = null;
                        window.__slidexCaptchaFail = null;
                        window.initAliyunCaptcha({
                            SceneId: args.scene,
                            mode: "popup",
                            // 语言白名单只有 cn/tw/en/…（SDK 内置 languageError）：
                            // "zh-CN" 2.7.66 实测被判非法，SDK 不初始化即降级 stub。
                            language: "cn",
                            showErrorTip: false,
                            element: "#cap-holder",
                            button: "#cap-btn",
                            getInstance: (instance) => {
                                try { instance.startTracelessVerification(); }
                                catch { finish("ARM_ERROR"); }
                            },
                            success: (p) => {
                                const v = typeof p === "string" ? p : (p && p.captchaVerifyParam) || null;
                                window.__slidexCaptchaParam = v;
                                finish(null);
                            },
                            fail: (e) => {
                                window.__slidexCaptchaFail = (e && e.verifyCode) || "FAIL";
                                finish(null);
                            },
                            onError: () => finish("ON_ERROR"),
                        });
                    } catch (e) { finish("EXC:" + e); }
                    setTimeout(() => finish("TIMEOUT"), 50000);
                })""",
                    {"scene": scene, "region": region, "prefix": prefix},
                ), timeout=55)
            except asyncio.TimeoutError:
                traceless = "TIMEOUT"
            _debug(f"attempt {attempt}: traceless={traceless!r}")
            # 关键分支修正（2026-11-18 20:44 实弹）：traceless 返回 "TIMEOUT"/
            # "ON_ERROR"/"ARM_ERROR" 等字符串时是**真值**，旧逻辑走 else 直接
            # 跳过拖拽——7 连 attempt 全只打无痕，ladder 一次未执行。字串只当
            # 归因，实体路径统一落拖拽兜底。
            traceless_fail = traceless if isinstance(traceless, str) else None
            if traceless_fail:
                traceless = None
            if not traceless:
                param = await page.evaluate("() => window.__slidexCaptchaParam || null")
                if _verify_param_ok(param):
                    print(f"VERIFY_PARAM={param}", flush=True)
                    _debug(f"ok via traceless (attempt {attempt})")
                    return 0
                if param:
                    _debug(f"traceless param degraded (len={len(param)}, "
                           f"no securityToken) — reject and retry")
                last_fail = f"TRACELESS:{await page.evaluate('() => window.__slidexCaptchaFail') or traceless_fail or ('DEGRADED' if param else 'NO_PARAM')}"
                _debug(f"traceless no param, fail={last_fail}")
                # 无痕被拒后 SDK 已弹 popup 兜底 → 落第二跳拟人拖拽。
                # 手势按 attempt 交替（2026-11-18 21:07 实弹）：同一 popup 内
                # real 判败后 SDK 立即关闭弹窗，同 attempt 再补 synthetic 只会
                # ARM（popup not visible，实测 5 连败）。改为各自独立 context：
                # 奇数发 synthetic（slidex 唯一端到端成功样本形态），偶数发
                # real（CDP 可信事件）。SLIDEX_DRAG=real|synthetic 可强制单形态。
                provider = provider_cls()
                attempt_drag = drag  # plan（缺省）| real（回滚）| synthetic（诊断）
                remaining = deadline_ms / 1000.0 - (_time.monotonic() - start)
                result = await _guarded(provider.acquire_verify_param(
                    page, scene,
                    region=region, prefix=prefix,
                    drag=attempt_drag,
                    max_attempts=1,
                    verdict_timeout_ms=10000,
                    deadline_s=max(25, int(remaining - 6)),
                    plan_factory=_plan_factory if attempt_drag == "plan" else None,
                ), max(38.0, remaining - 4), "DEADLINE")
                _debug(f"attempt {attempt}: drag={attempt_drag} "
                       f"fail={result.get('fail_code')}")
                if attempt_drag == "plan":
                    # 反馈码映射（_gestures 语义）：F001/F015 = 上游显式拒绝
                    # 回执 → 换家族；其余（ARM/NO_GAP/DRAG/TIMEOUT/EXC）=
                    # 无回执 → 同家族重采样。成功已在上面 return，不经过这里。
                    result_fail = result.get("fail_code")
                    feedback["code"] = (
                        UPSTREAM_REJECT_CODE if result_fail in ("F001", "F015")
                        else PACKET_MISS_CODE
                    )
                if _verify_param_ok(result.get("param")):
                    print(f"VERIFY_PARAM={result['param']}", flush=True)
                    _debug(f"ok via drag (attempt {attempt})")
                    return 0
                if result.get("param"):
                    _debug(f"drag param degraded (len={len(result['param'])}) — reject")
                last_fail = f"DRAG:{result.get('fail_code') or ('DEGRADED' if result.get('param') else 'NO_PARAM')}"
                if result.get("fail_code") == "NO_PLAN":
                    # 手势家族耗尽（plan 模式）：继续换 context 只会白烧预算并
                    # 给风控追加确认样本——整个 attempt 循环终止。
                    _debug("gesture session exhausted — stop attempt loop")
                    break
            _debug(f"attempt {attempt} failed: {last_fail}; retrying with fresh context")
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001 — 回收尽力而为
                pass
    print(f"SLIDEX_FAIL={last_fail or 'DEADLINE'}", flush=True)
    _debug(f"budget exhausted after {attempt} attempt(s)")
    return 1


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: slidex_runner.py <sceneId> <region> <prefix>", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_solve(sys.argv[1], sys.argv[2], sys.argv[3]))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — 顶层兜底：失败原因必须可见
        # 契约行带异常类型（2026-11-18 review P3）：裸 "EXC" 对外层归因零价值
        print(f"SLIDEX_FAIL=EXC:{type(exc).__name__}", flush=True)
        _debug(f"fatal: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
