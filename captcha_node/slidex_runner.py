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
    SLIDEX_DRAG           real（缺省，CDP 可信事件）| synthetic
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
    drag = os.environ.get("SLIDEX_DRAG", "real").strip().lower()
    _debug(f"scene={scene} region={region} prefix={prefix} proxy={'yes' if proxy else 'no'} "
           f"deadline={deadline_ms}ms drag={drag} exe={exe}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            executable_path=exe if Path(exe).exists() else None,
        )
        try:
            return await _attempt_in_browser(
                pw, browser, AliyunCaptchaJsProvider,
                scene, region, prefix, proxy, drag, deadline_ms,
            )
        finally:
            await browser.close()


async def _attempt_in_browser(
    pw, browser, provider_cls, scene, region, prefix, proxy, drag, deadline_ms
) -> int:
    import time as _time

    start = _time.monotonic()
    # F001/F015 归因重试（slidex 手册规范）：行为风控拒绝后换全新 context
    # （新指纹/新控件会话）再试，直至预算耗尽。
    last_fail = None
    attempt = 0  # 预算 ≤0 时循环不进，末尾 debug 引用不得 NameError
    while _time.monotonic() - start < deadline_ms / 1000.0:
        attempt += 1
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            proxy=proxy,
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
            traceless = await page.evaluate(
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
                    setTimeout(() => finish("TIMEOUT"), 45000);
                })""",
                {"scene": scene, "region": region, "prefix": prefix},
            )
            _debug(f"attempt {attempt}: traceless={traceless!r}")
            if not traceless:
                param = await page.evaluate("() => window.__slidexCaptchaParam || null")
                if _verify_param_ok(param):
                    print(f"VERIFY_PARAM={param}", flush=True)
                    _debug(f"ok via traceless (attempt {attempt})")
                    return 0
                if param:
                    _debug(f"traceless param degraded (len={len(param)}, "
                           f"no securityToken) — reject and retry")
                last_fail = f"TRACELESS:{await page.evaluate('() => window.__slidexCaptchaFail') or ('DEGRADED' if param else 'NO_PARAM')}"
                _debug(f"traceless no param, fail={last_fail}")
                # 无痕被拒后 SDK 已弹 popup 兜底 → 落第二跳拟人拖拽。
                # 手势 ladder（2026-11-18 19:4x 实弹）：real（CDP 可信事件）
                # 像素级命中（target=240.0px / readback sl='240px'）仍被
                # F001/F008/F015 连拒——上游拒的是轨迹形态本身；slidex 手册
                # 端到端唯一成功样本是 synthetic（页面内合成手势，纯前进无回
                # 拉）。故 real 判败后同 attempt 立即补一发 synthetic，两种
                # 形态都给上游一次机会，不再互相独占预算。
                provider = provider_cls()
                result = await provider.acquire_verify_param(
                    page, scene,
                    region=region, prefix=prefix,
                    drag=drag,
                    max_attempts=2,
                    verdict_timeout_ms=8000,
                    deadline_s=max(10, int(deadline_ms / 1000 - (_time.monotonic() - start))),
                )
                if not _verify_param_ok(result.get("param")) and drag == "real":
                    _debug(f"drag=real failed ({result.get('fail_code')}) — retry synthetic")
                    result2 = await provider.acquire_verify_param(
                        page, scene,
                        region=region, prefix=prefix,
                        drag="synthetic",
                        max_attempts=2,
                        verdict_timeout_ms=8000,
                        deadline_s=max(10, int(deadline_ms / 1000 - (_time.monotonic() - start))),
                    )
                    if _verify_param_ok(result2.get("param")):
                        result = result2
                    elif result2.get("param"):
                        _debug(f"synthetic param degraded (len={len(result2['param'])}) — reject")
                if _verify_param_ok(result.get("param")):
                    print(f"VERIFY_PARAM={result['param']}", flush=True)
                    _debug(f"ok via drag (attempt {attempt})")
                    return 0
                if result.get("param"):
                    _debug(f"drag param degraded (len={len(result['param'])}) — reject")
                last_fail = f"DRAG:{result.get('fail_code') or ('DEGRADED' if result.get('param') else 'NO_PARAM')}"
            else:
                last_fail = f"TRACELESS:{traceless}"
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
