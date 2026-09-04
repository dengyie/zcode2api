// solver.js — 无浏览器阿里云无痕验证求解器（Node + happy-dom）。
// 设计对齐 zapi captcha-happy.ts（设计移植，非代码拷贝）：
//   1. 常量指纹（Chrome/127 Linux + SwiftShader WebGL + 1px canvas）
//   2. alicdn 资源磁盘+内存双缓存（~/.zcode-captcha-cdn-cache/<sha1>）
//   3. pe.* 字节码 VM 补丁（btoa/atob 调用打 __DBT 观测钩子）
//   4. interceptor 全量接管网络层：每请求注入 client-hint/UA/origin/referer
//   5. guest 侧补丁（Event.isTrusted、HTMLDocument 命名、错误静默记录）
//   6. ~40 项浏览器 polyfill + native-toString 伪装 + 行为仿真（鼠标滑动）
//   7. 严格 extractVerifyParam：短参数/缺 securityToken 的降级结果直接拒绝
// 子进程模型：一进程一解，成功打印 VERIFY_PARAM=<param> 后退出。
// 用法: node solver.js <scene> <region> <prefix>
// 退出码: 0 成功 / 2 超时 / 3 初始化失败 / 4 fail / 5 onError / 6 参数无效

// 注意：必须用 Window（BrowserWindow 子类，setupVMContext 建 VM 上下文，
// guest 脚本里 window/document 才有定义）；GlobalWindow 走宿主 eval 且不建
// VM 上下文，Node 26 下 <script> 静默不执行（Bun 下 zapi 靠全局别名绕过）。
const { Window, PropertySymbol } = require("happy-dom");
const WindowBrowserContext =
  require("happy-dom/lib/window/WindowBrowserContext.js").default ||
  require("happy-dom/lib/window/WindowBrowserContext.js");
const { ProxyAgent, setGlobalDispatcher } = require("undici");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SCENE = process.argv[2] || "11xygtvd";
const REGION = process.argv[3] || "sgp";
const PREFIX = process.argv[4] || "no8xfe";

const DEBUG = /^(1|true|yes)$/i.test(process.env.CAPTCHA_DEBUG || "");
const dbg = (msg) => {
  if (DEBUG) process.stderr.write(`[solver] ${msg}\n`);
};

const proxyUrl = process.env.HTTP_PROXY || process.env.HTTPS_PROXY;
if (proxyUrl) {
  try {
    setGlobalDispatcher(new ProxyAgent(proxyUrl));
  } catch (_) {}
}

// ── 指纹（常量；与 zapi 同款 SwiftShader 形态，避免按次漂移）────────────────
function generateFingerprint() {
  const userAgent =
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36";
  return {
    userAgent,
    uaMajor: "127",
    uaFull: "127.0.0.0",
    platform: "Linux x86_64",
    screen: { w: 1280, h: 720, aw: 1280, ah: 720 },
    webglUnmaskedVendor: "Google Inc. (Google)",
    webglUnmaskedRenderer:
      "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)",
    canvasImage:
      "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  };
}
const fp = generateFingerprint();

const HTML = `<!DOCTYPE html><html><head></head><body>
<div id="cap"></div><button id="btn"></button>
<script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
</body></html>`;

// ── CDN 双缓存 ──────────────────────────────────────────────────────────────
const CDN_CACHE_DIR = path.join(os.homedir(), ".zcode-captcha-cdn-cache");
const _memCdnCache = new Map();
// 同一进程内一次求解只跑一遍，但 stall 记账按 URL 保留在模块级：
// 子进程池串行拉起时，上一进程的 stall 文件由磁盘 eviction 落实。
const _stallCounts = new Map();

function diskPathFor(url) {
  return path.join(CDN_CACHE_DIR, crypto.createHash("sha1").update(String(url)).digest("hex"));
}

function sniffMime(url) {
  if (/\.js(\?|$)/i.test(url)) return "application/javascript";
  if (/\.css(\?|$)/i.test(url)) return "text/css";
  if (/\.png(\?|$)/i.test(url)) return "image/png";
  if (/\.(jpg|jpeg)(\?|$)/i.test(url)) return "image/jpeg";
  if (/\.json(\?|$)/i.test(url)) return "application/json";
  return "application/octet-stream";
}

function getCachedBody(url) {
  const mem = _memCdnCache.get(url);
  if (mem) return mem;
  try {
    const p = diskPathFor(url);
    if (fs.existsSync(p)) {
      const body = fs.readFileSync(p);
      _memCdnCache.set(url, body);
      return body;
    }
  } catch (_) {}
  return null;
}

async function fetchAndStore(url) {
  try {
    const res = await fetch(url, { headers: { "user-agent": fp.userAgent } });
    const buf = Buffer.from(await res.arrayBuffer());
    if (buf.length > 0) {
      _memCdnCache.set(url, buf);
      try {
        const p = diskPathFor(url);
        fs.mkdirSync(CDN_CACHE_DIR, { recursive: true });
        fs.writeFileSync(p, buf);
        const stat = fs.statSync(p);
        if (stat.size !== buf.length) {
          fs.writeFileSync(p, buf);
        }
      } catch (err) {
        dbg(`cache-write-err ${url}: ${err.message}`);
      }
    }
    return buf;
  } catch (err) {
    dbg(`loader-fetch-err ${url}: ${err.message}`);
    return null;
  }
}

// stall（pe VM 卡死）处理：同 URL 连续两次即逐出内存+磁盘缓存，
// 下次 init 重新拉新版 pe 字节码（旋转版本可能整体坏掉）。
function noteStallAndMaybeEvict(peUrl) {  try {
    if (!peUrl || !/dynamicJS\//.test(peUrl)) return;
    const n = (_stallCounts.get(peUrl) || 0) + 1;
    _stallCounts.set(peUrl, n);
    process.stderr.write(`[pe-stall] ${peUrl.split("/").pop()} x${n}\n`);
    if (n >= 2) {
      _memCdnCache.delete(peUrl);
      try {
        fs.unlinkSync(diskPathFor(peUrl));
      } catch (_) {}
      _stallCounts.delete(peUrl);
    } else if (fs.existsSync(diskPathFor(peUrl))) {
      // 子进程内没有"下次"，直接当次失效：删缓存让下一次进程拉新
      _memCdnCache.delete(peUrl);
      try {
        fs.unlinkSync(diskPathFor(peUrl));
      } catch (_) {}
    }
  } catch (_) {}
}

// ── pe.* 字节码 VM 观测钩子（btoa/atob 调用入栈时记录，排障用）───────────────
const peVmCallRegex =
  /55==A\?\(f=r\[n\+\+\],l=e\.pop\(\),h=e\.pop\(\),o=\[\],\w+\(f\)\.forEach\(function\(\)\{o\.unshift\(e\.pop\(\)\)\}\),p=null===h\?l\.apply\((\w+),o\):h\[l\]\.apply\(h,o\),r\[n\+\+\]&&e\.push\(p\)\):/;
function patchPeBundle(buf, url) {
  if (process.env.PE_PATCH === "off") return buf;
  if (!/dynamicJS\/[^/]*\/pe\.\d+\./.test(url)) return buf;
  let src = buf.toString("utf8");
  if (src.includes("__DBT")) return buf;
  const m = src.match(peVmCallRegex);
  if (!m) return buf;
  const locals = m[1];
  const hook = `55==A?(f=r[n++],l=e.pop(),h=e.pop(),o=[],v(f).forEach(function(){o.unshift(e.pop())}),p=null===h?l.apply(${locals},o):h[l].apply(h,o),r[n++]&&e.push(p),function(){try{if(l===window.btoa||l===window.atob){window.__DBT=window.__DBT||[];var __sav=[];for(var __i=0;__i<e.length;__i++){var __vv=e[__i];if(typeof __vv==="string"){__sav.push("s:"+__vv)}else if(typeof __vv==="number"){__sav.push("n:"+__vv)}else if(typeof __vv==="boolean"){__sav.push("b:"+__vv)}else if(__vv&&typeof __vv.length==="number"){__sav.push("a:"+__vv.length)}else{__sav.push("t:"+typeof __vv)}}var __ls={};for(var __k2 in ${locals}){if(__k2!=="_"&&__k2!=="*"&&__k2!=="arguments"){try{var __lv=${locals}[__k2];if(typeof __lv==="string"){__ls[__k2]="s:"+__lv}else if(typeof __lv==="number"){__ls[__k2]="n:"+__lv}else if(__lv&&typeof __lv.length==="number"){__ls[__k2]="a:"+__lv.length}else{__ls[__k2]="t:"+typeof __lv}}catch(_e){}}}window.__DBT.push({call:"btoa",ip:n,args:o.map(function(__a){return typeof __a==="string"?"s:"+__a:typeof __a==="number"?"n:"+__a:typeof __a==="function"?"fn:"+(__a.name||"?"):typeof __a==="object"&&__a?"obj":typeof __a}),stack:__sav,locals:__ls,rlen:r.length,r:r})}}catch(_e){}}()):`;
  src = src.replace(m[0], hook);
  dbg(`loader-patch ${url} (VM hook applied, locals=${locals})`);
  return Buffer.from(src, "utf8");
}

// ── 每请求头注入（XHR/fetch/script 全走这里）────────────────────────────────
function injectRequestHeaders(request) {
  const h = request.headers;
  try {
    h.set("sec-ch-ua", '"Chromium";v="' + fp.uaMajor + '", "Not)A;Brand";v="24"');
    h.set("sec-ch-ua-mobile", "?0");
    h.set("sec-ch-ua-platform", '"Linux"');
    h.set("user-agent", fp.userAgent);
    h.set("accept-language", "en-US,en;q=0.9");
    h.set("referer", "https://zcode.z.ai/");
    let origin = null;
    try {
      const u = new URL(request.url);
      const method = String(request.method || "GET").toUpperCase();
      const crossOrigin = u.origin !== "https://zcode.z.ai";
      if (crossOrigin || (method !== "GET" && method !== "HEAD")) {
        origin = "https://zcode.z.ai";
      }
    } catch (_) {}
    if (origin) h.set("origin", origin);
  } catch (_) {}
}

function cookieHeader(request) {
  try {
    const ctx = global.__cookieContainer;
    if (!ctx || request.credentials === "omit") return null;
    const u = new URL(request.url);
    const cookies = ctx.getCookies(u, false);
    if (cookies.length > 0) {
      return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
    }
  } catch (_) {}
  return null;
}

function cookiesFromSetCookie(list, url) {
  const cookieContainer = global.__cookieContainer;
  if (!cookieContainer || !list || !list.length) return;
  for (const raw of list) {
    try {
      const u = new URL(url);
      const parts = raw.split(";");
      const pair = parts[0].split("=");
      const cookie = {
        name: pair[0].trim(),
        value: pair.slice(1).join("=").trim(),
        url: u.origin,
        domain: u.hostname,
        path: "/",
      };
      for (const p of parts.slice(1)) {
        const kv = p.trim().split(/=(.*)/s);
        const k = (kv[0] || "").toLowerCase();
        if (k === "domain" && kv[1]) cookie.domain = kv[1];
        if (k === "path" && kv[1]) cookie.path = kv[1];
        if (k === "expires") cookie.expires = new Date(kv[1]).getTime();
        if (k === "max-age") cookie.maxAge = parseInt(kv[1], 10);
        if (k === "httponly") cookie.httpOnly = true;
        if (k === "secure") cookie.secure = true;
        if (k === "samesite") cookie.sameSite = kv[1];
      }
      cookieContainer.addCookies([cookie]);
    } catch (_) {}
  }
}

// ── 自有 sync fetch（替换 happy-dom 坏掉的 spawn） ───────────────────────────
// happy-dom v20 的 SyncFetchScriptBuilder 生成 `http.request(undefined, opts,
// cb)`，Node 26 下直接 TypeError（URL 为 undefined 时 opts 被当 listener）。
// 拦截器对 sync 请求永不返回 null：缓存未命中时用下面这个正确的子进程拉取。
function syncFetchBody(url, headers) {
  const script = `
const http = require("http"), https = require("https");
const u = new URL(${JSON.stringify(url)});
const mod = u.protocol === "https:" ? https : http;
const req = mod.request(u, { method: "GET", headers: ${JSON.stringify(headers || {})}, rejectUnauthorized: false }, (res) => {
  const chunks = [];
  res.on("data", (c) => chunks.push(c));
  res.on("end", () => console.log(JSON.stringify({ error: null, status: res.statusCode, b64: Buffer.concat(chunks).toString("base64") })));
});
req.on("error", (e) => console.log(JSON.stringify({ error: e.message })));
req.end();`;
  try {
    const out = require("node:child_process").execFileSync(process.argv[0], ["-e", script], {
      encoding: "buffer",
      timeout: 20000,
      maxBuffer: 256 * 1024 * 1024,
    });
    const parsed = JSON.parse(out.toString("utf8"));
    if (parsed.error) {
      dbg(`sync-fetch err ${url}: ${parsed.error}`);
      return null;
    }
    return Buffer.from(parsed.b64, "base64");
  } catch (err) {
    dbg(`sync-fetch spawn fail ${url}: ${err.message}`);
    return null;
  }
}

// ── interceptor：整体替换 happy-dom 网络层 ──────────────────────────────────
function makeInterceptor() {
  async function serveCdn(url, w) {
    if (!/\balicdn\.com/i.test(url)) return null;
    let body = getCachedBody(url);
    if (body && /\.js(\?|$)/i.test(url)) {
      try {
        new Function(body.toString("utf8"));
      } catch (parseErr) {
        process.stderr.write(`[cache-bad-js] ${url} len=${body.length} ${parseErr.message} — refetch\n`);
        _memCdnCache.delete(url);
        try {
          fs.unlinkSync(diskPathFor(url));
        } catch (_) {}
        body = null;
      }
    }
    if (!body) body = await fetchAndStore(url);
    if (!body) return null;
    if (/dynamicJS\/[^/]*\/pe\.\d+\./.test(url)) {
      try {
        w.__lastPeUrl = url;
      } catch (_) {}
    }
    return { body, mime: sniffMime(url) };
  }

  return {
    async beforeAsyncRequest({ request, window: w }) {
      const url = request.url;
      global.__requestLog.push({ at: Date.now(), method: request.method, url });
      injectRequestHeaders(request);
      const cdn = await serveCdn(url, w);
      if (cdn) {
        return new w.Response(patchPeBundle(Buffer.from(cdn.body), url), {
          status: 200,
          statusText: "OK",
          headers: { "content-type": cdn.mime },
        });
      }
      // 非 CDN：直连（undici fetch，尊重全局 ProxyAgent）
      try {
        const init = { method: request.method, headers: {} };
        request.headers.forEach((value, key) => {
          init.headers[key] = value;
        });
        const cookie = cookieHeader(request);
        if (cookie) init.headers.cookie = cookie;
        let hasBody = false;
        try {
          if (request.body) {
            const ab = await request.arrayBuffer();
            if (ab && ab.byteLength > 0) {
              init.body = ab;
              hasBody = true;
            }
          }
        } catch (_) {}
        const res = await fetch(url, init);
        const buf = Buffer.from(await res.arrayBuffer());
        cookiesFromSetCookie(
          typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [],
          url,
        );
        const bs = new URL(url);
        if (DEBUG && /captcha-open|verify\.|device\.saf|cloudauth-device|upload\./i.test(url) && buf.length < 4096) {
          process.stderr.write(`[xhr] ${request.method} ${bs.hostname}${bs.pathname} -> ${res.status} ${buf.toString("utf8").slice(0, 1200)}\n`);
        } else {
          dbg(`xhr ${request.method} ${bs.hostname}${bs.pathname} -> ${res.status} (${buf.length}b)`);
        }
        const headers = {};
        const ct = res.headers.get("content-type");
        if (ct) headers["content-type"] = ct;
        return new w.Response(buf, {
          status: res.status,
          statusText: res.statusText || "",
          headers,
        });
      } catch (err) {
        dbg(`xhr-err ${url}: ${err.message}`);
        return new w.Response("", { status: 503, statusText: "passthrough failed" });
      }
    },
    beforeSyncRequest({ request, window: w }) {
      // sync XHR/脚本：缓存命中直接回；未命中走自有 syncFetchBody（happy-dom
      // 自带的 node -e spawn 脚本在 Node 26 下是坏的，绝不放行到它）。
      const url = request.url;
      global.__requestLog.push({ at: Date.now(), method: request.method, url, sync: true });
      injectRequestHeaders(request);
      const isCdn = /\balicdn\.com/i.test(url);
      if (isCdn) {
        const cached = getCachedBody(url);
        if (cached) {
          if (/dynamicJS\/[^/]*\/pe\.\d+\./.test(url)) {
            try {
              w.__lastPeUrl = url;
            } catch (_) {}
          }
          return {
            status: 200,
            statusText: "OK",
            ok: true,
            url,
            redirected: false,
            headers: new w.Headers({ "content-type": sniffMime(url) }),
            body: patchPeBundle(Buffer.from(cached), url),
            [PropertySymbol.virtualServerFile]: null,
          };
        }
        // 未命中：同步拉取并回填缓存（首个 solve 预热 AliyunCaptcha.js 走这里）
        const fresh = syncFetchBody(url, { "user-agent": fp.userAgent });
        if (fresh && fresh.length > 0) {
          _memCdnCache.set(url, fresh);
          try {
            fs.mkdirSync(CDN_CACHE_DIR, { recursive: true });
            fs.writeFileSync(diskPathFor(url), fresh);
          } catch (_) {}
          return {
            status: 200,
            statusText: "OK",
            ok: true,
            url,
            redirected: false,
            headers: new w.Headers({ "content-type": sniffMime(url) }),
            body: patchPeBundle(fresh, url),
            [PropertySymbol.virtualServerFile]: null,
          };
        }
        return null;
      }
      // 非 CDN sync 请求（通常不发生：XHR 都是异步）：自有 sync fetch 兜底，
      // 保证不落到 happy-dom 坏掉的 spawn。
      const cookie = cookieHeader(request);
      const hdrs = { "user-agent": fp.userAgent };
      if (cookie) hdrs.cookie = cookie;
      const fresh = syncFetchBody(url, hdrs);
      if (fresh && fresh.length > 0) {
        return {
          status: 200,
          statusText: "OK",
          ok: true,
          url,
          redirected: false,
          headers: new w.Headers({ "content-type": "application/json" }),
          body: fresh,
          [PropertySymbol.virtualServerFile]: null,
        };
      }
      return null;
    },
  };
}

// ── parse-fail 观测：happy-dom VM eval 漏斗包装 ─────────────────────────────
function installEvalInstrumentation(w) {
  const sym = PropertySymbol && PropertySymbol.evaluateScript;
  if (!sym || typeof w[sym] !== "function") {
    dbg("no evaluateScript symbol, host hook skipped");
    return;
  }
  const orig = w[sym];
  w[sym] = function (code, options) {
    try {
      return orig.call(this, code, options);
    } catch (err) {
      try {
        const src = String(code || "");
        const filename = (options && options.filename) || "?";
        const sha1 = crypto.createHash("sha1").update(src).digest("hex");
        process.stderr.write(
          `\n[EVAL-PARSE-FAIL] file=${filename} len=${src.length} sha1=${sha1}\n` +
            `  head300: ${JSON.stringify(src.slice(0, 300))}\n` +
            `  err: ${err && err.message}\n`,
        );
        if (/^https?:/.test(filename)) {
          (async () => {
            try {
              const fresh = await fetchAndStore(filename);
              if (fresh && fresh.length !== src.length) {
                dbg(`EVAL-CACHE-MISMATCH deleting ${diskPathFor(filename)}`);
                try {
                  fs.unlinkSync(diskPathFor(filename));
                } catch (_) {}
                _memCdnCache.delete(filename);
              }
            } catch (_) {}
          })();
        }
      } catch (_) {}
      throw err;
    }
  };
}

// ── JS 实现的平台 API 伪装成 native（FeiLin toString 扫描对抗）───────────────
function installNativeToString(w) {
  const realToString = Function.prototype.toString;
  const nativeRe = /\[native code\]/;
  const mask = (fn) => {
    if (typeof fn !== "function") return;
    try {
      if (nativeRe.test(realToString.call(fn))) return;
      const name = fn.name || "";
      const nativeStr = `function ${name}() { [native code] }`;
      Object.defineProperty(fn, "toString", {
        value: () => nativeStr,
        configurable: true,
        writable: true,
      });
    } catch (_) {}
  };
  const seen = new w.Set();
  const maskObj = (obj, depth) => {
    if (!obj || (typeof obj !== "object" && typeof obj !== "function") || depth > 5) return;
    try {
      if (obj.constructor && obj.constructor.prototype !== Object.prototype) {
        const ctorName = obj.constructor.name;
        if (/^(WriteStream|ReadStream|Socket|Process|Timeout|Immediate)$/.test(ctorName)) return;
      }
    } catch (_) {}
    if (seen.has(obj)) return;
    try {
      seen.add(obj);
    } catch (_) {
      return;
    }
    let names = [];
    try {
      names = Object.getOwnPropertyNames(obj);
    } catch (_) {
      return;
    }
    for (const name of names) {
      if (name === "toString" || name === "constructor") continue;
      let desc;
      try {
        desc = Object.getOwnPropertyDescriptor(obj, name);
      } catch (_) {
        continue;
      }
      if (!desc) continue;
      if (typeof desc.value === "function") {
        mask(desc.value);
      } else if (typeof desc.get === "function") {
        mask(desc.get);
        try {
          const v = desc.get.call(obj);
          if (typeof v === "function") mask(v);
        } catch (_) {}
      }
      if (depth < 3) {
        try {
          const v = desc.value;
          if (v && (typeof v === "function" || typeof v === "object")) maskObj(v, depth + 1);
        } catch (_) {}
      }
    }
  };
  const targets = [
    w,
    w.navigator,
    w.document,
    w.Document && w.Document.prototype,
    w.Element && w.Element.prototype,
    w.HTMLElement && w.HTMLElement.prototype,
    w.Node && w.Node.prototype,
    w.EventTarget && w.EventTarget.prototype,
    w.HTMLCanvasElement && w.HTMLCanvasElement.prototype,
    w.XMLHttpRequest && w.XMLHttpRequest.prototype,
    w.Event && w.Event.prototype,
    w.Window && w.Window.prototype,
  ].filter(Boolean);
  for (const t of targets) {
    try {
      maskObj(t, 0);
    } catch (_) {}
  }
}

// ── guest 侧补丁（window.eval 注入 VM realm）────────────────────────────────
const GUEST_EVAL_PATCH = `
(function() {
  try {
    Object.defineProperty(Event.prototype, "isTrusted", {
      get() { return true; },
      configurable: true
    });
  } catch (e) {}
  try {
    if (window.HTMLDocument) {
      Object.defineProperty(window.HTMLDocument, "name", { value: "HTMLDocument", configurable: true });
      Object.defineProperty(window.HTMLDocument.prototype, Symbol.toStringTag, { value: "HTMLDocument", configurable: true });
    }
  } catch (e) {}
  try {
    Object.defineProperty(window.Document.prototype, Symbol.toStringTag, { value: "HTMLDocument", configurable: true });
  } catch (e) {}
  // guest 错误只记录不打印：SDK 在 happy-dom 里每次求解都会抛良性
  // TypeError（DOM 仿真不完美），但求解仍能成功 —— 失败时才随错误带出。
  function __capRecord(kind, msg, stack) {
    try {
      var m = String(msg || "?");
      var s = String(stack || "").split("\\n").slice(0, 2).join(" | ");
      if (!window.__capErrs) window.__capErrs = [];
      var last = window.__capErrs[window.__capErrs.length - 1];
      if (last && last.k === kind && last.m === m) {
        last.n = (last.n || 1) + 1;
      } else {
        window.__capErrs.push({ k: kind, m: m, s: s, n: 1 });
        if (window.__capErrs.length > 8) window.__capErrs.shift();
      }
      if (${DEBUG ? "true" : "false"}) console.error("[" + kind + "]", m, s);
    } catch (e2) {}
  }
  try {
    window.addEventListener("unhandledrejection", function(e) {
      var r = e && e.reason;
      __capRecord("UH-REASON", (r && r.message) || typeof r, r && r.stack);
    });
  } catch (e) {}
  try {
    window.addEventListener("error", function(e) {
      __capRecord("WINDOW-ERROR", e && e.message, e && e.error && e.error.stack);
    });
  } catch (e) {}
  // eval/Function parse-fail 观测（pe 动态 chunk 走这里）
  function __capFailDump(kind, code) {
    try {
      var src = String(code || "");
      console.error("[" + kind + "] url=" + (window.__lastPeUrl || "?") + " len=" + src.length + " head=" + JSON.stringify(src.slice(0, 300)) + " tail=" + JSON.stringify(src.slice(-100)));
    } catch (e2) {}
  }
  try {
    var _origEval2 = window.eval;
    if (_origEval2) {
      window.eval = function(code) {
        try { return _origEval2.call(window, code); }
        catch (e) {
          if (e && (/unexpected|invalid|parse|syntax/i.test(String((e && e.message) || e)))) {
            __capFailDump("REALM-EVAL-FAIL", code);
          }
          throw e;
        }
      };
    }
  } catch (e) {}
  try {
    var _of = window.Function;
    if (_of) {
      var _WF = function() {
        var args = Array.prototype.slice.call(arguments);
        var body = args.length ? String(args[args.length - 1]) : "";
        try { return _of.apply(this, args); }
        catch (e) {
          if (e && (/unexpected|invalid|parse|syntax/i.test(String((e && e.message) || e)))) {
            __capFailDump("REALM-FN-FAIL", body);
          }
          throw e;
        }
      };
      _WF.prototype = _of.prototype;
      try { Object.defineProperty(_WF, "name", { value: "Function", configurable: true }); } catch (e) {}
      window.Function = _WF;
    }
  } catch (e) {}
})();
`;

// ── 浏览器 polyfill 集（FeiLin / pe 风控引擎探测面）─────────────────────────
function applyPolyfills(w) {
  if (typeof w.Option !== "function") {
    w.Option = class Option extends w.HTMLOptionElement {
      constructor(text, value, defaultSelected, selected) {
        super();
        if (text !== undefined) {
          const el = w.document.createElement("option");
          el.text = text;
          if (value !== undefined) el.value = value;
          if (defaultSelected) el.defaultSelected = true;
          if (selected) el.selected = true;
          return el;
        }
      }
    };
  }
  if (typeof w.Video !== "function" && w.HTMLVideoElement) {
    w.Video = class Video extends w.HTMLVideoElement {
      constructor() {
        return w.document.createElement("video");
      }
    };
  }

  if (typeof w.alert !== "function") w.alert = () => {};
  if (typeof w.prompt !== "function") w.prompt = () => null;
  if (typeof w.confirm !== "function") w.confirm = () => false;
  try {
    Object.defineProperty(w, "alert", { value: () => {}, configurable: true, writable: true });
    Object.defineProperty(w, "prompt", { value: () => null, configurable: true, writable: true });
    Object.defineProperty(w, "confirm", { value: () => false, configurable: true, writable: true });
    // happy-dom 自带 open/close 有破坏性（close 会拆窗），一律中和
    Object.defineProperty(w, "open", { value: () => null, configurable: true, writable: true });
    Object.defineProperty(w, "close", { value: () => {}, configurable: true, writable: true });
  } catch (_) {}

  const extraGlobals = {
    print: () => {},
    stop: () => {},
    moveTo: () => {},
    moveBy: () => {},
    showModalDialog: () => null,
    find: () => false,
  };
  for (const [k, v] of Object.entries(extraGlobals)) {
    try {
      Object.defineProperty(w, k, { value: v, configurable: true, writable: true });
    } catch (_) {}
  }

  if (!w.EventSource) {
    w.EventSource = class {
      constructor() {
        this.readyState = 2;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
      }
      close() {
        this.readyState = 2;
      }
      addEventListener() {}
      removeEventListener() {}
    };
  }

  if (!w.Beacon) w.Beacon = class {};

  if (!w.RTCPeerConnection) {
    w.RTCPeerConnection = class {
      constructor() {}
      createDataChannel() {
        return {};
      }
      close() {}
      createOffer() {
        return Promise.resolve({});
      }
      setLocalDescription() {
        return Promise.resolve();
      }
      addEventListener() {}
      removeEventListener() {}
    };
  }

  if (!w.MessageChannel) {
    w.MessageChannel = class {
      constructor() {
        this.port1 = { onmessage: null, postMessage() {}, start() {}, close() {}, addEventListener() {}, removeEventListener() {} };
        this.port2 = { onmessage: null, postMessage() {}, start() {}, close() {}, addEventListener() {}, removeEventListener() {} };
      }
    };
  }

  w.IntersectionObserver =
    w.IntersectionObserver ||
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
    };

  w.ResizeObserver =
    w.ResizeObserver ||
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };

  w.DeviceOrientationEvent =
    w.DeviceOrientationEvent ||
    class extends w.Event {
      constructor(type, opts) {
        super(type, opts);
      }
    };
  w.DeviceMotionEvent =
    w.DeviceMotionEvent ||
    class extends w.Event {
      constructor(type, opts) {
        super(type, opts);
      }
    };

  w.requestIdleCallback =
    w.requestIdleCallback ||
    ((cb) => w.setTimeout(() => cb({ didTimeout: false, timeRemaining: () => 10 }), 1));
  w.cancelIdleCallback = w.cancelIdleCallback || ((id) => w.clearTimeout(id));

  w.matchMedia =
    w.matchMedia ||
    (() => ({
      matches: false,
      media: "",
      onchange: null,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
      dispatchEvent() {
        return false;
      },
    }));

  if (!w.visualViewport) {
    const VisualViewport = function () {};
    VisualViewport.prototype = {
      width: fp.screen.w - 16,
      height: fp.screen.h - 120,
      scale: 1,
      offsetLeft: 0,
      offsetTop: 0,
      pageLeft: 0,
      pageTop: 0,
      onresize: null,
      onscroll: null,
      onscrollend: null,
    };
    w.VisualViewport = VisualViewport;
    w.visualViewport = Object.create(w.VisualViewport.prototype);
  }

  if (!w.indexedDB) {
    const IDBFactory = function () {};
    IDBFactory.prototype = {
      open: () => ({ onupgradeneeded: null, onsuccess: null, onerror: null }),
      deleteDatabase: () => ({}),
      databases: () => Promise.resolve([]),
    };
    w.IDBFactory = IDBFactory;
    w.indexedDB = Object.create(w.IDBFactory.prototype);
  }

  if (!w.speechSynthesis) {
    const SpeechSynthesis = function () {};
    SpeechSynthesis.prototype = {
      speak() {},
      cancel() {},
      pause() {},
      resume() {},
      getVoices: () => [],
    };
    w.SpeechSynthesis = SpeechSynthesis;
    w.speechSynthesis = Object.create(w.SpeechSynthesis.prototype);
    w.SpeechSynthesisUtterance = function () {};
  }

  w.Worker =
    w.Worker ||
    class {
      postMessage() {}
      terminate() {}
      addEventListener() {}
      removeEventListener() {}
    };

  w.Notification =
    w.Notification ||
    class {
      static permission = "default";
      static requestPermission() {
        return Promise.resolve("default");
      }
      close() {}
    };

  // ── Canvas / WebGL ──
  const proto = w.HTMLCanvasElement.prototype;
  const nativeGetContext = typeof proto.getContext === "function" ? proto.getContext : null;
  proto.getContext = function (type, ...rest) {
    if (/webgl/i.test(type)) {
      return makeWebGLMock(this);
    }
    if (nativeGetContext) {
      try {
        const ctx = nativeGetContext.call(this, type, ...rest);
        if (ctx) return ctx;
      } catch (_) {}
    }
    return make2DStub(this);
  };

  function makeWebGLMock(canvas) {
    return {
      canvas,
      getParameter(p) {
        if (p === 7936) return "WebKit";
        if (p === 7937) return "WebKit WebGL";
        if (p === 7938) return "WebGL 1.0 (OpenGL ES 2.0 Chromium)";
        if (p === 35724) return "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)";
        if (p === 0x9245) return fp.webglUnmaskedVendor;
        if (p === 0x9246) return fp.webglUnmaskedRenderer;
        return "Intel Inc.";
      },
      getExtension(name) {
        if (name === "WEBGL_debug_renderer_info") {
          return { UNMASKED_VENDOR_WEBGL: 0x9245, UNMASKED_RENDERER_WEBGL: 0x9246 };
        }
        return null;
      },
      getSupportedExtensions() {
        return [
          "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
          "EXT_disjoint_timer_query", "EXT_float_blend", "EXT_frag_depth",
          "EXT_shader_texture_lod", "EXT_texture_compression_bptc",
          "EXT_texture_compression_rgtc", "EXT_texture_filter_anisotropic",
          "EXT_sRGB", "KHR_parallel_shader_compile", "OES_element_index_uint",
          "OES_fbo_render_mipmap", "OES_standard_derivatives",
          "OES_texture_float", "OES_texture_float_linear",
          "OES_texture_half_float", "OES_texture_half_float_linear",
          "OES_vertex_array_object", "WEBGL_color_buffer_float",
          "WEBGL_compressed_texture_astc", "WEBGL_compressed_texture_etc",
          "WEBGL_compressed_texture_etc1", "WEBGL_compressed_texture_s3tc",
          "WEBGL_compressed_texture_s3tc_srgb", "WEBGL_debug_renderer_info",
          "WEBGL_debug_shaders", "WEBGL_depth_texture", "WEBGL_draw_buffers",
          "WEBGL_lose_context", "WEBGL_multi_draw",
        ];
      },
      getContextAttributes() {
        return {
          alpha: true, antialias: true, depth: true,
          failIfMajorPerformanceCaveat: false, powerPreference: "default",
          premultipliedAlpha: true, preserveDrawingBuffer: false,
          stencil: false, desynchronized: false,
        };
      },
      getShaderPrecisionFormat() {
        return { precision: 23, rangeMin: 127, rangeMax: 127 };
      },
    };
  }

  function make2DStub(canvas) {
    return {
      canvas,
      fillRect() {},
      clearRect() {},
      getImageData: (_x, _y, w2 = 1, h2 = 1) => new w.ImageData(w2, h2),
      putImageData() {},
      createImageData: (w2 = 1, h2 = 1) => new w.ImageData(w2, h2),
      setTransform() {},
      transform() {},
      drawImage() {},
      save() {},
      restore() {},
      beginPath() {},
      moveTo() {},
      lineTo() {},
      bezierCurveTo() {},
      quadraticCurveTo() {},
      closePath() {},
      clip() {},
      stroke() {},
      fill() {},
      arc() {},
      rect() {},
      ellipse() {},
      translate() {},
      scale() {},
      rotate() {},
      fillText() {},
      strokeText() {},
      measureText: (t) => ({ width: String(t).length * 8 }),
      createLinearGradient: () => ({ addColorStop() {} }),
      createRadialGradient: () => ({ addColorStop() {} }),
      createPattern: () => ({}),
      isPointInPath: () => false,
      font: "10px sans-serif",
      textBaseline: "alphabetic",
      textAlign: "start",
      fillStyle: "#000",
      strokeStyle: "#000",
      globalAlpha: 1,
      lineWidth: 1,
      shadowBlur: 0,
      shadowColor: "",
    };
  }

  const nativeToDataURL = typeof proto.toDataURL === "function" ? proto.toDataURL : null;
  proto.toDataURL = function (...a) {
    try {
      if (nativeToDataURL) return nativeToDataURL.apply(this, a);
    } catch (_) {}
    return fp.canvasImage;
  };
  if (typeof proto.toBlob !== "function") {
    proto.toBlob = (cb) => cb && cb(new w.Blob());
  }

  w.OffscreenCanvas =
    w.OffscreenCanvas ||
    class {
      constructor(width, height) {
        this.width = width;
        this.height = height;
      }
      getContext() {
        return proto.getContext.call(this);
      }
    };

  // ── Audio（确定性正弦渲染，指纹稳定）──
  const audioMock = class {
    constructor() {
      this.sampleRate = 44100;
      this.currentTime = 0;
      this.state = "suspended";
    }
    createOscillator() {
      return {
        type: "sine",
        frequency: { value: 440, setValueAtTime() {} },
        connect() {},
        start() {},
        stop() {},
      };
    }
    createDynamicsCompressor() {
      return {
        threshold: { value: -24, setValueAtTime() {} },
        knee: { value: 30, setValueAtTime() {} },
        ratio: { value: 12, setValueAtTime() {} },
        attack: { value: 0.003, setValueAtTime() {} },
        release: { value: 0.25, setValueAtTime() {} },
        connect() {},
      };
    }
    createAnalyser() {
      return {
        fftSize: 2048,
        frequencyBinCount: 1024,
        getByteFrequencyData() {},
        getByteTimeDomainData() {},
        connect() {},
      };
    }
    createGain() {
      return { gain: { value: 1 }, connect() {} };
    }
    destination = {};
    resume() {
      this.state = "running";
      return Promise.resolve();
    }
    close() {
      this.state = "closed";
      return Promise.resolve();
    }
  };
  w.AudioContext = w.AudioContext || audioMock;
  w.OfflineAudioContext =
    w.OfflineAudioContext ||
    class extends audioMock {
      constructor(_channels, length, sampleRate) {
        super();
        this.length = length;
        this.sampleRate = sampleRate;
      }
      startRendering() {
        const len = this.length || 44100;
        const sr = this.sampleRate || 44100;
        const buf = new Float32Array(len);
        for (let i = 0; i < len; i += 1) {
          const t = i / sr;
          buf[i] =
            Math.sin(2 * Math.PI * 1000 * t) * Math.exp(-t * 1.2) * 0.6 +
            Math.sin(2 * Math.PI * 3000 * t) * Math.exp(-t * 1.5) * 0.25 +
            Math.sin(2 * Math.PI * 5000 * t) * Math.exp(-t * 2.0) * 0.12;
        }
        return Promise.resolve({
          numberOfChannels: 1,
          length: len,
          sampleRate: sr,
          getChannelData: () => buf,
        });
      }
    };

  w.requestAnimationFrame = w.requestAnimationFrame || ((cb) => w.setTimeout(() => cb(Date.now()), 16));
  w.cancelAnimationFrame = w.cancelAnimationFrame || ((id) => w.clearTimeout(id));

  try {
    Object.defineProperty(w.document, "hidden", { value: false, configurable: true });
    Object.defineProperty(w.document, "visibilityState", { value: "visible", configurable: true });
  } catch (_) {}

  if (!w.document.fonts) {
    w.document.fonts = {
      ready: Promise.resolve(),
      check: () => true,
      addEventListener() {},
      removeEventListener() {},
    };
  }

  // ── chrome 对象 ──
  if (!w.chrome) {
    w.chrome = {
      app: {
        isInstalled: false,
        InstallState: { DISABLED: "disabled", INSTALLED: "installed", NOT_INSTALLED: "not_installed" },
        RunningState: { CANNOT_RUN: "cannot_run", CAN_RUN: "can_run", RUNNING: "running" },
        getDetails() {
          return null;
        },
        getIsInstalled() {
          return false;
        },
        installState(cb) {
          if (cb) cb("not_installed");
        },
        runningState(cb) {
          if (cb) cb("cannot_run");
        },
      },
      csi() {
        const now = Date.now();
        return { startE: now - 100, onloadT: now, pageT: 100, tran: 15 };
      },
      loadTimes() {
        const now = Date.now() / 1000;
        return {
          requestTime: now - 0.1, startLoadTime: now - 0.1,
          commitLoadTime: now - 0.05, finishDocumentLoadTime: now,
          finishLoadTime: now, firstPaintTime: now - 0.02,
          firstPaintAfterLoadTime: 0, navigationType: "Other",
          wasFetchedViaSpdy: true, wasNpnNegotiated: true,
          npnNegotiatedProtocol: "h2", wasAlternateProtocolAvailable: false,
          connectionInfo: "h2",
        };
      },
    };
  }

  // ── navigator ──
  const nav = w.navigator;
  const plugins = createNavigatorPlugins(w);
  const navPatch = {
    userAgent: fp.userAgent,
    platform: fp.platform,
    language: "en-US",
    languages: ["en-US", "en"],
    vendor: "Google Inc.",
    webdriver: false,
    hardwareConcurrency: 12,
    deviceMemory: 8,
    maxTouchPoints: 0,
    cookieEnabled: true,
    plugins: plugins.plugins,
    mimeTypes: plugins.mimeTypes,
    appVersion: fp.userAgent.replace(/^Mozilla\//, ""),
    appName: "Netscape",
    appCodeName: "Mozilla",
    product: "Gecko",
    productSub: "20030107",
    vendorSub: "",
    doNotTrack: null,
    sendBeacon: (url, data) => {
      try {
        const xhr = new w.XMLHttpRequest();
        xhr.open("POST", url, true);
        xhr.send(data);
        return true;
      } catch (_) {
        return false;
      }
    },
  };
  for (const [k, v] of Object.entries(navPatch)) {
    try {
      Object.defineProperty(nav, k, { value: v, configurable: true });
    } catch (_) {}
  }

  const makeNS = (protoObj) => {
    const C = new w.Function();
    C.prototype = protoObj;
    return new C();
  };

  if (!nav.connection) {
    const NetInfo = () => {};
    NetInfo.prototype = { onchange: null, effectiveType: "4g", rtt: 50, downlink: 10, saveData: false };
    w.NetworkInformation = NetInfo;
    try {
      Object.defineProperty(nav, "connection", { value: makeNS(NetInfo.prototype), configurable: true });
    } catch (_) {}
  }
  if (!nav.userAgentData) {
    const UAData = function () {};
    UAData.prototype = {
      brands: [
        { brand: "Chromium", version: fp.uaMajor },
        { brand: "Not)A;Brand", version: "24" },
      ],
      mobile: false,
      platform: "Linux",
      getHighEntropyValues: () =>
        Promise.resolve({
          brands: [
            { brand: "Chromium", version: fp.uaMajor },
            { brand: "Not)A;Brand", version: "24" },
          ],
          mobile: false,
          platform: "Linux",
          platformVersion: "6.5.0",
          architecture: "x86",
          model: "",
          uaFullVersion: fp.uaFull,
          fullVersionList: [
            { brand: "Chromium", version: fp.uaFull },
            { brand: "Not)A;Brand", version: "24.0.0.0" },
          ],
        }),
    };
    try {
      Object.defineProperty(nav, "userAgentData", { value: makeNS(UAData.prototype), configurable: true });
    } catch (_) {}
  }
  if (!w.Permissions) {
    const Perms = () => {};
    Perms.prototype = {
      query: (param) =>
        Promise.resolve({ state: param.name === "notifications" ? "prompt" : "granted", onchange: null }),
    };
    w.Permissions = Perms;
  }
  try {
    if (!nav.permissions) Object.defineProperty(nav, "permissions", { value: makeNS(w.Permissions.prototype), configurable: true });
  } catch (_) {}
  try {
    if (!nav.clipboard)
      Object.defineProperty(nav, "clipboard", {
        value: makeNS({ readText: () => Promise.resolve(""), writeText: () => Promise.resolve() }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.geolocation)
      Object.defineProperty(nav, "geolocation", {
        value: makeNS({
          getCurrentPosition: (s) => s && s({ coords: { latitude: 0, longitude: 0, accuracy: 1 } }),
          watchPosition: () => 1,
          clearWatch: () => {},
        }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.credentials)
      Object.defineProperty(nav, "credentials", {
        value: makeNS({ get: () => Promise.resolve(null), create: () => Promise.resolve(null), store: () => Promise.resolve(), preventSilentAccess: () => Promise.resolve() }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.storage)
      Object.defineProperty(nav, "storage", {
        value: makeNS({ estimate: () => Promise.resolve({ quota: 1e8, usage: 0 }), persisted: () => Promise.resolve(false), persist: () => Promise.resolve(false) }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.usb)
      Object.defineProperty(nav, "usb", {
        value: makeNS({ getDevices: () => Promise.resolve([]), requestDevice: () => Promise.reject(new Error("no devices")) }),
        configurable: true,
      });
  } catch (_) {}
  try {
    if (!nav.mediaDevices)
      Object.defineProperty(nav, "mediaDevices", {
        value: makeNS({ enumerateDevices: () => Promise.resolve([]), getUserMedia: () => Promise.reject(new Error("NotAllowedError")) }),
        configurable: true,
      });
  } catch (_) {}

  // ── screen / 窗口尺寸 ──
  const screenPatch = {
    width: fp.screen.w,
    height: fp.screen.h,
    availWidth: fp.screen.w,
    availHeight: fp.screen.ah,
    availLeft: 0,
    availTop: 0,
    colorDepth: 24,
    pixelDepth: 24,
    orientation: { angle: 0, type: "landscape-primary", onchange: null },
  };
  for (const [k, v] of Object.entries(screenPatch)) {
    try {
      Object.defineProperty(w.screen, k, { get: () => v, configurable: true });
    } catch (_) {}
  }

  w.outerWidth = fp.screen.w;
  w.outerHeight = fp.screen.h - 40;
  w.innerWidth = fp.screen.w - 16;
  w.innerHeight = fp.screen.h - 120;
  w.devicePixelRatio = 1;
}

function createNavigatorPlugins(w) {
  const indexed = [
    { name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format" },
    { name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai", description: "" },
    { name: "Chromium PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai", description: "" },
  ];
  const plugins = w.PluginArray ? Object.create(w.PluginArray.prototype) : {};
  const mockIndexed = [];
  for (let i = 0; i < indexed.length; i++) {
    const p = Object.create((w.Plugin && w.Plugin.prototype) || Object.prototype);
    Object.defineProperty(p, "name", { value: indexed[i].name, configurable: true, enumerable: true });
    Object.defineProperty(p, "filename", { value: indexed[i].filename, configurable: true, enumerable: true });
    Object.defineProperty(p, "description", { value: indexed[i].description, configurable: true, enumerable: true });
    Object.defineProperty(p, "length", { value: 1, configurable: true, enumerable: true });
    Object.defineProperty(p, "0", { value: p, configurable: true, enumerable: true });
    p.item = () => p;
    p.namedItem = () => p;
    plugins[i] = p;
    mockIndexed.push(p);
  }
  Object.defineProperty(plugins, "length", { value: indexed.length, configurable: true, enumerable: true });
  plugins.item = (i) => plugins[i] ?? null;
  plugins.namedItem = (name) => mockIndexed.find((p) => p.name === name) ?? null;
  plugins.refresh = () => {};
  const mimeTypes = w.MimeTypeArray ? Object.create(w.MimeTypeArray.prototype) : {};
  Object.defineProperty(mimeTypes, "length", { value: 0, configurable: true, enumerable: true });
  mimeTypes.item = () => null;
  mimeTypes.namedItem = () => null;
  return { plugins, mimeTypes };
}

// ── 行为仿真（FeiLin 人类动作缓冲：600ms 鼠标滑行 + 点击 + 键盘）─────────────
function simulateBehavior(w, durationMs = 600) {
  const { document, MouseEvent, KeyboardEvent, UIEvent } = w;
  if (!document || !MouseEvent) return;
  const fire = (type, ctor, opts) => {
    try {
      const Ctor = ctor || UIEvent;
      const ev = new Ctor(type, { bubbles: true, cancelable: true, view: w, ...opts });
      document.dispatchEvent(ev);
      if (document.body) document.body.dispatchEvent(ev);
    } catch (_) {}
  };
  let x = 140 + Math.random() * 30;
  let y = 110 + Math.random() * 20;
  const targetX = 540 + Math.random() * 40;
  const targetY = 380 + Math.random() * 30;
  const steps = 22;
  let i = 0;
  const start = Date.now();
  const moveStep = () => {
    if (i > steps) return;
    x += (targetX - x) * 0.16 + (Math.random() - 0.5) * 5;
    y += (targetY - y) * 0.16 + (Math.random() - 0.5) * 4;
    fire("mousemove", MouseEvent, {
      screenX: Math.round(x),
      screenY: Math.round(y),
      clientX: Math.round(x),
      clientY: Math.round(y),
      button: 0,
      buttons: 1,
    });
    i += 1;
    const done = Date.now() - start >= durationMs;
    if (i <= steps && !done) {
      w.setTimeout(moveStep, 26 + Math.floor(Math.random() * 32));
    } else {
      fire("mousedown", MouseEvent, { clientX: Math.round(x), clientY: Math.round(y), button: 0, buttons: 1 });
      fire("mouseup", MouseEvent, { clientX: Math.round(x), clientY: Math.round(y), button: 0, buttons: 0 });
      fire("click", MouseEvent, { clientX: Math.round(x), clientY: Math.round(y), button: 0 });
      try {
        fire("keyup", KeyboardEvent, { key: "a", code: "KeyA", keyCode: 65, which: 65 });
      } catch (_) {}
    }
  };
  moveStep();
}

// ── cookie priming 缓存（对齐 zapi _cookieCache，5 分钟） ─────────────────────
const COOKIE_CACHE_TTL_MS = 5 * 60 * 1000;
let _cookieCache = { cookies: [], ts: 0 };

// ── waitFor：轮询等待条件成立（initAliyunCaptcha 挂载） ──────────────────────
function waitFor(cond, timeoutMs, intervalMs = 50) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      let ok = false;
      try {
        ok = !!cond();
      } catch (_) {
        ok = false;
      }
      if (ok) return resolve(true);
      if (Date.now() - start >= timeoutMs) {
        return reject(new Error("waitFor timeout"));
      }
      setTimeout(tick, intervalMs);
    };
    tick();
  });
}

// ── createDom：装配一个可解验证码的 happy-dom 窗口 ──────────────────────────
// 对齐 zapi createDom：cookie priming（5min 缓存）→ GlobalWindow（开启 JS 执行 +
// 拦截器）→ WindowBrowserContext 取 cookieContainer → 预置 cookie → 装 polyfill/
// 掩码/eval 探针（务必在 SDK 脚本执行前）→ document.write(HTML) → 挂 config。
async function createDom(region, prefix) {
  let cookies = [];
  const now = Date.now();
  if (_cookieCache.ts > 0 && now - _cookieCache.ts < COOKIE_CACHE_TTL_MS) {
    cookies = _cookieCache.cookies;
  } else {
    try {
      const res = await fetch("https://zcode.z.ai/", {
        headers: {
          "User-Agent": fp.userAgent,
          "sec-ch-ua": '"Chromium";v="' + fp.uaMajor + '", "Not)A;Brand";v="24"',
          "sec-ch-ua-mobile": "?0",
          "sec-ch-ua-platform": '"Linux"',
          "Accept-Language": "en-US,en;q=0.9",
        },
      });
      cookies = typeof res.headers.getSetCookie === "function" ? res.headers.getSetCookie() : [];
      _cookieCache = { cookies, ts: Date.now() };
    } catch (_) {}
  }

  const interceptor = makeInterceptor();

  // 子进程只解一次，无需 unhandledRejection 累积防护，但要防止 guest 脚本抛出的
  // 同步异常终止整个进程（happy-dom 的异常观察者会 rethrow）。
  if (!process.__capUncaughtHooked) {
    process.__capUncaughtHooked = true;
    process.on("uncaughtException", (err) => {
      dbg(`[guest-uncaught] ${err && err.message ? err.message : String(err)}`);
    });
    process.on("unhandledRejection", (reason) => {
      dbg(`[guest-unhandledRejection] ${reason && reason.message ? reason.message : String(reason)}`);
    });
  }

  const noop = () => {};
  const guestConsole = DEBUG
    ? {
        log: (...a) => process.stderr.write(`[guest-log] ${a.map(String).join(" ")}\n`),
        warn: (...a) => process.stderr.write(`[guest-warn] ${a.map(String).join(" ")}\n`),
        error: (...a) => process.stderr.write(`[guest-err] ${a.map(String).join(" ")}\n`),
        info: (...a) => process.stderr.write(`[guest-info] ${a.map(String).join(" ")}\n`),
        debug: (...a) => process.stderr.write(`[guest-debug] ${a.map(String).join(" ")}\n`),
        trace: noop,
      }
    : { log: noop, warn: noop, error: noop, info: noop, debug: noop, trace: noop };

  const w = new Window({
    url: "https://zcode.z.ai/",
    console: guestConsole,
    settings: {
      enableJavaScriptEvaluation: true,
      enableImageFileLoading: true,
      suppressInsecureJavaScriptEnvironmentWarning: true,
      navigator: { userAgent: fp.userAgent },
      viewport: { width: fp.screen.w, height: fp.screen.h, devicePixelRatio: 1 },
      fetch: {
        disableSameOriginPolicy: true,
        interceptor,
      },
    },
  });

  const browserFrame = new WindowBrowserContext(w).getBrowserFrame();
  global.__browserFrame = browserFrame;
  global.__cookieContainer = browserFrame.page.context.cookieContainer;

  // priming cookie（从 zcode.z.ai 首页 set-cookie 灌入）
  for (const raw of cookies) {
    try {
      const u = new URL("https://zcode.z.ai/");
      const parts = raw.split(";");
      const pair = parts[0].split("=");
      const cookie = {
        name: pair[0].trim(),
        value: pair.slice(1).join("=").trim(),
        url: u.origin,
        domain: u.hostname,
        path: "/",
      };
      for (const p of parts.slice(1)) {
        const kv = p.trim().split(/=(.*)/s);
        const k = (kv[0] || "").toLowerCase();
        if (k === "domain" && kv[1]) cookie.domain = kv[1];
        if (k === "path" && kv[1]) cookie.path = kv[1];
        if (k === "expires") cookie.expires = new Date(kv[1]).getTime();
        if (k === "max-age") cookie.maxAge = parseInt(kv[1], 10);
        if (k === "httponly") cookie.httpOnly = true;
        if (k === "secure") cookie.secure = true;
        if (k === "samesite") cookie.sameSite = kv[1];
      }
      browserFrame.page.context.cookieContainer.addCookies([cookie]);
    } catch (_) {}
  }

  // 预置访客身份 cookie（对齐 zapi）
  const visitorId = crypto.randomUUID();
  const deviceMid = crypto.randomUUID();
  const pre = [
    { name: "zcode_visitor_id", value: visitorId, domain: "zcode.z.ai" },
    { name: "zcode_device_mid", value: deviceMid, domain: "zcode.z.ai" },
    { name: "visitor_id", value: visitorId, domain: "zcode.z.ai", httpOnly: true },
  ];
  for (const c of pre) {
    try {
      browserFrame.page.context.cookieContainer.addCookies([{ ...c, url: "https://zcode.z.ai", path: "/" }]);
    } catch (_) {}
  }

  // polyfill / 掩码 / eval 探针必须在 SDK 脚本执行前装好
  applyPolyfills(w);
  installNativeToString(w);
  installEvalInstrumentation(w);
  if (w.Error) {
    try {
      w.Error.prepareStackTrace = Error.prepareStackTrace;
    } catch (_) {}
  }

  // guest 侧 dump 助手调用的宿主回调：算 sha1、比对 pe 磁盘缓存新鲜度
  w.__capDebugDump = (url, src, kind) => {
    if (!DEBUG) return;
    try {
      const s = String(src || "");
      const sha1 = crypto.createHash("sha1").update(s).digest("hex");
      process.stderr.write(
        `\n[${kind}] url=${url} len=${s.length} sha1=${sha1}\n` +
          `  head300: ${JSON.stringify(s.slice(0, 300))}\n`,
      );
    } catch (_) {}
  };

  w.eval(GUEST_EVAL_PATCH);
  w.document.write(HTML);
  w.AliyunCaptchaConfig = { region, prefix };

  return { window: w, browserFrame };
}

// ── 结果提取：严格校验（对齐 zapi extractVerifyParam） ──────────────────────
// 真实 verifyParam ~280 字符 base64-JSON，内含 securityToken（≥50 字符）。
// len-76 的降级结果（无 securityToken）上游必然 3007，绝不放行。
function extractVerifyParam(param) {
  let verifyParam = param;
  if (param && typeof param === "object") {
    verifyParam = param.verifyParam || param.data || param.param;
  }
  if (!verifyParam || String(verifyParam).length < 20) {
    throw new Error("solver returned empty param: " + JSON.stringify(param));
  }
  const str = String(verifyParam);
  if (str.length < 200) {
    throw new Error(
      "verify param too short (" + str.length + " chars) — degraded result: " + str.slice(0, 80),
    );
  }
  try {
    const decoded = JSON.parse(Buffer.from(str, "base64").toString("utf8"));
    const secTok = decoded && (decoded.securityToken || decoded.SecurityToken);
    if (!secTok || String(secTok).length < 50) {
      throw new Error("verify param missing securityToken — degraded result: " + str.slice(0, 80));
    }
  } catch (err) {
    if (err instanceof SyntaxError) {
      throw new Error("verify param not base64-JSON: " + str.slice(0, 80));
    }
    throw err;
  }
  return str;
}

function handleCaptchaResult(result) {
  if (result && typeof result === "object" && result.verifyResult === false) {
    throw new Error(
      "verify rejected: " +
        JSON.stringify({ verifyCode: result.verifyCode, certifyId: result.certifyId }),
    );
  }
  return result;
}

// ── main：单次求解 → 打印 VERIFY_PARAM= ─────────────────────────────────────
async function main() {
  global.__requestLog = [];
  global.__cookieContainer = null;
  global.__browserFrame = null;

  const timeoutMs = 30_000;
  const stallMs = Number(process.env.CAPTCHA_STALL_MS || 6_000);

  const dom = await createDom(REGION, PREFIX);
  const w = dom.window;
  const solveStart = Date.now();

  try {
    await waitFor(() => typeof w.initAliyunCaptcha === "function", timeoutMs, 50);
    simulateBehavior(w, 600);

    const param = await new Promise((resolve, reject) => {
      const reqSnapshot = () =>
        global.__requestLog
          .filter((r) => r.at >= solveStart)
          .map((r) => `${r.at - solveStart}ms ${r.method} ${String(r.url).replace(/^https?:\/\//, "").slice(0, 60)}`)
          .slice(-12);

      const timer = setTimeout(() => {
        const peUrl = (() => {
          try {
            return w.__lastPeUrl || "?";
          } catch (_) {
            return "?";
          }
        })();
        reject(new Error(`captcha solve timeout pe=${peUrl.split("/").pop() || peUrl} reqs=${JSON.stringify(reqSnapshot())}`));
      }, timeoutMs);

      // 失速探测：健康求解会持续发 XHR（~3s 内到 verify）。若 stallMs 内无新
      // XHR，则该 pe-VM 版本失速 —— 提前中止并淘汰缓存，让上层重试拿新 pe。
      const stallTimer = setInterval(() => {
        const last = global.__requestLog[global.__requestLog.length - 1];
        if (last && Date.now() - last.at > stallMs) {
          const peUrl = (() => {
            try {
              return w.__lastPeUrl || "?";
            } catch (_) {
              return "?";
            }
          })();
          noteStallAndMaybeEvict(peUrl);
          clearTimeout(timer);
          clearInterval(stallTimer);
          reject(new Error(`captcha solve stall pe=${peUrl.split("/").pop() || peUrl} lastXhr=${last.at - solveStart}ms reqs=${JSON.stringify(reqSnapshot())}`));
        }
      }, 500);

      const finish = (fn) => (value) => {
        clearTimeout(timer);
        clearInterval(stallTimer);
        fn(value);
      };

      try {
        w.initAliyunCaptcha({
          SceneId: SCENE,
          mode: "popup",
          region: REGION,
          prefix: PREFIX,
          language: "en",
          element: "#cap",
          button: "#btn",
          captchaLogoImg: "",
          showErrorTip: false,
          getInstance: (inst) => {
            try {
              (inst.startTracelessVerification || inst.show).call(inst);
            } catch (e) {
              finish(reject)(new Error(`start: ${e.message}`));
            }
          },
          success: (result) => {
            try {
              finish(resolve)(handleCaptchaResult(result));
            } catch (err) {
              finish(reject)(err);
            }
          },
          fail: (err) => finish(reject)(new Error(`fail: ${JSON.stringify(err)}`)),
          onError: (err) => finish(reject)(new Error(`onError: ${JSON.stringify(err)}`)),
        });
      } catch (err) {
        clearTimeout(timer);
        clearInterval(stallTimer);
        reject(err);
      }
    });

    // 成功后清理该 pe 的失速计数
    try {
      const okPe = w.__lastPeUrl;
      if (okPe) _stallCounts.delete(okPe);
    } catch (_) {}

    const out = extractVerifyParam(param);
    process.stdout.write("VERIFY_PARAM=" + out + "\n");
    process.exit(0);
  } catch (err) {
    // 附带 guest 错误摘要（仅失败时可用）
    let summary = "";
    try {
      const errs = w && w.__capErrs;
      if (errs && errs.length) {
        summary =
          " guestErrors: " +
          errs
            .slice(0, 4)
            .map((e) => `${(e && e.k) || "?"}: ${String((e && e.m) || "?").slice(0, 100)}`)
            .join(" || ");
      }
    } catch (_) {}
    const msg = (err && err.message ? err.message : String(err)) + summary;
    dbg("[solve-fail] " + msg);

    // 退出码分类：超时 2 / init 失败 3 / fail 4 / onError 5 / 参数无效 6 / 其它 4
    let code = 4;
    if (/timeout|stall/.test(msg)) code = 2;
    else if (/waitFor timeout/.test(msg)) code = 3;
    else if (/^fail:|fail:/.test(msg)) code = 4;
    else if (/onError:/.test(msg)) code = 5;
    else if (/verify param|degraded|securityToken|base64/.test(msg)) code = 6;
    process.exit(code);
  } finally {
    try {
      const cap = w.document.getElementById("cap");
      if (cap) cap.replaceChildren();
      w.happyDOM.close();
    } catch (_) {}
    global.__cookieContainer = null;
    global.__browserFrame = null;
  }
}

main().catch((err) => {
  dbg("[main-fatal] " + (err && err.message ? err.message : String(err)));
  process.exit(3);
});
