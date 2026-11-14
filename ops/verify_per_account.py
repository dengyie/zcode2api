import importlib.util
import json
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location(
    "swap", "/personal/tebi-zcode-hub/zcode-egress-swap.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

cspec = importlib.util.spec_from_file_location(
    "zconstants", "/data/zcode-hub/app/app/constants.py")
zc = importlib.util.module_from_spec(cspec)
cspec.loader.exec_module(zc)
import uuid


def billing_headers(acc_data):
    fp = acc_data["fingerprint"]
    oscat = "windows" if fp["platform"] in ("win32", "windows") else (
        "macos" if fp["platform"] in ("darwin", "macos") else "linux")
    return {
        "Content-Type": "application/json",
        "User-Agent": "ZCode/%s" % zc.BILLING_APP_VERSION,
        "HTTP-Referer": zc.ZCODE_ORIGIN,
        "X-Title": zc.BILLING_TITLE,
        "X-ZCode-App-Version": zc.BILLING_APP_VERSION,
        "X-Platform": "%s-%s" % (fp["platform"], fp["arch"]),
        "X-Release-Channel": zc.BILLING_RELEASE_CHANNEL,
        "X-Client-Language": fp["language"],
        "X-Client-Timezone": fp["timezone"],
        "X-Os-Category": oscat,
        "X-Os-Version": fp["os_version"],
        "X-Device-Mid": fp["device_mid"],
        "x-request-id": str(uuid.uuid4()),
        "Authorization": "Bearer " + acc_data["jwt_token"],
    }

con = m.sqlite3.connect(m.DB, uri=True)
rows = con.execute(
    "SELECT id, name, data FROM accounts WHERE enabled=1 "
    "ORDER BY created_at").fetchall()
con.close()


def via(url, method="GET", body=None, headers=None, proxy=None, timeout=40):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        r = op.open(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


results = []
for acc_id, name, blob in rows:
    d = json.loads(blob)
    egress_url = d.get("egress")
    if not egress_url or not d.get('jwt_token'):
        print(f"!! {name} ({acc_id}) 未绑定出口或无 JWT——跳过")
        continue
    port = egress_url.rsplit(":", 1)[1]

    # 1) 出口 IP（该 listener 实际出口）
    st, raw = via("http://ip-api.com/json/?fields=query,isp",
                  proxy=egress_url, timeout=15)
    try:
        ip = json.loads(raw).get("query", "?")
    except Exception:
        ip = "?"

    # 2) billing/balance（该账号 JWT × 该账号出口，保真指纹头）——billing 风控接受度
    st_b, raw_b = via("https://zcode.z.ai/api/v1/zcode-plan/billing/balance",
                      headers=billing_headers(d),
                      proxy=egress_url, timeout=40)
    try:
        code_b = json.loads(raw_b).get("code", "?")
    except Exception:
        code_b = "?"

    # 3) messages 边缘探针（该账号 JWT × 该账号出口）——JSON 3012=边缘放行
    st_m, raw_m = via(
        "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages", "POST",
        json.dumps({"model": "GLM-5.3-Flash", "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hi"}]}),
        headers={"Authorization": "Bearer " + d["jwt_token"],
                 "Content-Type": "application/json",
                 "User-Agent": "ZCode/3.17.2"},
        proxy=egress_url, timeout=40)
    low = raw_m.lstrip().lower()
    kind = "HTML-EDGE" if (low.startswith("<!doctype") or "<html" in low[:200]) else "JSON-APP"

    ok = (st_b == 200 and code_b == 0 and kind == "JSON-APP")
    results.append((name, acc_id[-8:], port, ip, st_b, code_b, st_m, kind, ok))
    print(f"{'PASS' if ok else 'FAIL'} {name:24s} …{acc_id[-8:]} :{port} exit={ip:16s} "
          f"billing={st_b}/code={code_b} messages={st_m}/{kind}")

ips = [r[3] for r in results]
print()
print(f"隔离性: {len(set(ips))}/{len(ips)} 个互异出口 IP")
print(f"可用性: {sum(1 for r in results if r[8])}/{len(results)} 对 (账号×IP) 双平面全过")
if len(set(ips)) != len(ips):
    print("!! 存在 IP 重复——隔离被破坏")
