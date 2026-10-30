// solver_pw.js — 真浏览器阿里云无痕验证求解器（puppeteer-core + 系统 Chromium）。
//
// 背景：happy-dom 路线（solver.js）自 2026-09 起被上游风控以「unusual activity」
// 全拒——阿里云无痕验证（traceless）信任真实浏览器环境（真渲染 / 真 JS 引擎 /
// 真设备信号），模拟 DOM 伪造的 token 一律判 bot。本求解器对齐 zcode-switch
// src/captcha.js 的形态：在真 Chromium 里加载官方 AliyunCaptcha SDK →
// initAliyunCaptcha(mode:popup) → startTracelessVerification() → success 回调
// 收 captchaVerifyParam。真环境下无痕验证自动通过，无需滑块。
//
// 子进程模型：一进程一解，成功打印 VERIFY_PARAM=<param> 后退出。
// 用法: node solver_pw.js <scene> <region> <prefix>
// 退出码: 0 成功 / 3 初始化失败 / 4 fail / 5 onError / 6 参数无效 / 7 内存不足
//
// 运维要点（pxed 实测，2026-10-01）：
//   - 容器内多进程 Chromium 起不来（helper 进程受限），--single-process 是
//     实测唯一能跑通的形态；重 JS 下偶发闪退由调用方重试（CAPTCHA_SOLVE_RETRIES）兜底。
//   - Chromium 在 Linux 自设 oom_score_adj=800，内存压力下总是它先被 OOM 杀：
//     启动前把自身归零（子进程继承），launch 后再对浏览器进程补一次。
//   - launch 需较长超时（内存压力下 Chromium 启动可到 30s+），默认 90s。

const path = require("node:path");
const fs = require("node:fs");

const [scene, region, prefix] = process.argv.slice(2);
if (!scene) {
  process.stderr.write("usage: solver_pw.js <scene> <region> <prefix>\n");
  process.exit(6);
}

const DEBUG = /^(1|true|yes)$/i.test(process.env.CAPTCHA_DEBUG || "");
const dbg = (m) => { if (DEBUG) process.stderr.write(`[pw] ${m}\n`); };

// ── Chromium 进程清理 ─────────────────────────────────────────────────────────
// 实测（Linux 容器）：Chromium 启动后会 setsid 脱离 node 的进程组（PGID=SESS=自身），
// 调用方对 node 进程组 killpg 打不到它，必须在 node 侧主动清理：
//   · process.on("exit")：正常退出 / exit(code) / 未捕获异常时同步补杀；
//   · 硬超时：早于调用方（captcha.py）超时 20s 自我了断，防止 NAS 小内存盒上的
//     挂死（实测可挂 25 分钟+）迫使调用方 SIGKILL，留下清不掉的逃逸 Chromium。
let chromePid = null;
function killChrome() {
  if (!chromePid) return;
  try { process.kill(-chromePid, "SIGKILL"); } catch { /* 未成组长，按单进程杀 */ }
  try { process.kill(chromePid, "SIGKILL"); } catch { /* 已退出 */ }
}
process.on("exit", killChrome);
const HARD_DEADLINE_MS = Number(process.env.ZCODE_SOLVE_DEADLINE_MS) || 210_000;
setTimeout(() => {
  process.stderr.write("[pw] 硬超时，清场退出\n");
  killChrome();
  process.exit(2);
}, HARD_DEADLINE_MS);

// ── 内存门：可用内存不足时不启 Chromium（每个实例数百 MB），快速退出等下轮 ──
function memAvailableMB() {
  try {
    const line = fs.readFileSync("/proc/meminfo", "utf8")
      .split("\n").find((l) => l.startsWith("MemAvailable:"));
    return line ? parseInt(line.replace(/\D+/g, ""), 10) : Infinity;
  } catch {
    return Infinity; // 非 Linux（本地开发）不设门
  }
}
if (memAvailableMB() < 500) {
  process.stderr.write("[pw] MemAvailable < 500MB，跳过本轮求解\n");
  process.exit(7);
}

// ── Chromium 定位：env 优先，其次常见路径 ────────────────────────────────────
function findChromium() {
  const candidates = [
    process.env.ZCODE_CHROMIUM_PATH,
    "/usr/local/bin/chromium",
    "/usr/local/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
  ].filter(Boolean);
  for (const c of candidates) {
    try { fs.accessSync(c, fs.constants.X_OK); return c; } catch { /* next */ }
  }
  return null;
}

let puppeteer;
try {
  puppeteer = require("puppeteer-core");
} catch (err) {
  process.stderr.write(`[pw] puppeteer-core 未安装（captcha_node 下 npm install）: ${err.message}\n`);
  process.exit(3);
}

// 对齐 hub 账号档案（win32-x64）——UA/平台与 billing 头一致，避免自相矛盾的风控信号。
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36";

const PAGE_URL = "file://" + path.join(__dirname, "page.html");

// 内存紧张形态下的 Chromium 启动参数（pxed 实测唯一能跑通的组合）
function chromeArgs(proxy) {
  const args = [
    "--no-sandbox", "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=site-per-process",
    "--lang=zh-CN",
    "--single-process", "--no-zygote", "--renderer-process-limit=1",
    "--disable-gpu", "--disable-software-rasterizer", "--disable-extensions",
    "--disable-background-networking", "--disable-default-apps",
    "--disable-component-update", "--disable-sync", "--no-first-run",
    "--memory-pressure-off", "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
  ];
  if (proxy) args.push("--proxy-server=" + proxy);
  return args;
}

// oom_score_adj 归零（尽力而为；失败不影响求解）
function clearOomScore(pid) {
  try { fs.writeFileSync(`/proc/${pid}/oom_score_adj`, "0"); } catch { /* 尽力 */ }
}

// oom 看门狗：Chromium 启动时会自设 oom_score_adj=800（Linux 渲染进程惯例），
// 共享小内存盒上内核一有压力就第一个杀它（dmesg 实证反复被杀）。launch 后以
// 150ms 周期持续压回 0，让 OOM killer 转而选择真正的泄漏大户（loongcollector，
// 其守护会自动重启）。返回停表函数。
function oomWatchdog(pid) {
  clearOomScore(pid);
  const t = setInterval(() => clearOomScore(pid), 150);
  if (t.unref) t.unref();
  return () => clearInterval(t);
}

(async () => {
  clearOomScore(process.pid); // Chromium 继承此值

  const executablePath = findChromium();
  if (!executablePath) {
    process.stderr.write("[pw] 未找到可执行 Chromium（设 ZCODE_CHROMIUM_PATH）\n");
    process.exit(3);
  }
  const proxy = process.env.HTTP_PROXY || process.env.HTTPS_PROXY || "";

  // --single-process 在共享小内存盒上约有一半概率闪退/启动失败（好坏窗口交替，
  // 与代理无关；dmesg 实证是 OOM killer 按 chrome 自设的 adj=800 优先杀它）。
  // 进程内自旋重试 + oom 看门狗压制，比让调用方换进程重试便宜得多。
  for (let attempt = 1; attempt <= 3; attempt++) {
    if (memAvailableMB() < 500) {
      process.stderr.write(`[pw] 第 ${attempt}/3 次：MemAvailable < 500MB，等待后重试\n`);
      await new Promise((r) => setTimeout(r, 8000));
      continue;
    }
    try {
      const param = await solveOnce(executablePath, proxy, scene, region, prefix);
      if (param && param.trim()) {
        console.log("VERIFY_PARAM=" + param.trim());
        process.exit(0);
      }
      process.stderr.write(`[pw] 第 ${attempt}/3 次未产出 verifyParam\n`);
    } catch (e) {
      process.stderr.write(`[pw] 第 ${attempt}/3 次异常: ${(e && e.message) || e}\n`);
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  process.exit(4);
})().catch((e) => {
  process.stderr.write("[pw] ERR " + (e && e.message) + "\n");
  process.exit(5);
});

async function solveOnce(executablePath, proxy, scene, region, prefix) {
  dbg(`launch ${executablePath}`);
  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: chromeArgs(proxy),
    defaultViewport: { width: 1280, height: 720 },
    protocolTimeout: 60_000,
    timeout: 90_000,
  });
  chromePid = browser.process() ? browser.process().pid : null;
  const stopWatchdog = oomWatchdog(browser.process() ? browser.process().pid : process.pid);
  try {
    const page = await browser.newPage();
    await page.setUserAgent(UA, {
      architecture: "x86", bitness: "64", mobile: false, model: "",
      platform: "Windows", platformVersion: "10.0.0", wow64: false,
    });
    await page.evaluateOnNewDocument(() => {
      Object.defineProperty(navigator, "webdriver", { get: () => undefined });
      Object.defineProperty(navigator, "languages", { get: () => ["zh-CN", "zh", "en"] });
      Object.defineProperty(navigator, "platform", { get: () => "Win32" });
    });
    dbg("goto " + PAGE_URL);
    await page.goto(PAGE_URL, { waitUntil: "networkidle2", timeout: 30_000 });
    await page.waitForFunction("typeof window.initAliyunCaptcha === 'function'", { timeout: 20_000 });
    dbg("SDK ready");

    return await page.evaluate(async (scene, region, prefix) => {
      window.AliyunCaptchaConfig = { region, prefix };
      return await new Promise((resolve) => {
        let done = false;
        const finish = (v) => { if (!done) { done = true; resolve(v || null); } };
        try {
          window.initAliyunCaptcha({
            SceneId: scene,
            mode: "popup",
            language: "zh-CN",
            showErrorTip: false,
            element: "#cap-holder",
            button: "#cap-btn",
            getInstance: (instance) => {
              try { instance.startTracelessVerification(); }
              catch { finish(null); }
            },
            success: (p) => {
              const v = typeof p === "string" ? p : p && p.captchaVerifyParam;
              finish(v);
            },
            fail: () => finish(null),
            onError: () => finish(null),
          });
        } catch { finish(null); }
        setTimeout(() => finish(null), 20_000);
      });
    }, scene, region, prefix);
  } finally {
    stopWatchdog();
    let closed = false;
    try { await browser.close(); closed = true; } catch { /* 退出码不受影响 */ }
    // close 成功才清空；失败时保留 pid 让 exit 钩子补杀（防逃逸残留）
    if (closed) chromePid = null;
  }
}
