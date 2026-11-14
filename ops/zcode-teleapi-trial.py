#!/usr/bin/env python3
"""zcode-teleapi-trial · teleapi(Bright Data) 出口 × zcode 上游 3 天风控实测（需求单 §11-2）。

每 30min 一拍、trial-01/02 轮换（exclude_node 排 pigeonw+另一 trial 节点 → 确定性钉住），
经 :9101 网关以独立绑定账号 zcode-hub-trial 走 teleapi 出口探两个平面：
  - messages：JSON(code=3012)=边缘放行（形状预期值） / HTML-EDGE=边缘封禁
  - billing：保真指纹头（fingerprint + constants），200=风控接受；429/3007/unusual=风控拒
结果落 JSONL（/personal/tebi-zcode-hub/zcode-teleapi-trial-results.jsonl）。
72h 后进入静默收尾（每 6h 打一次摘要行）。生产流量零影响（mihomo 路径不变，
trial 用独立绑定账号，探针流量微乎其微）。
"""

import importlib.util
import json
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

_spec = importlib.util.spec_from_file_location(
    "swap", "/personal/tebi-zcode-hub/zcode-egress-swap.py")
swap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(swap)

_cspec = importlib.util.spec_from_file_location(
    "zconstants", "/data/zcode-hub/app/app/constants.py")
zc = importlib.util.module_from_spec(_cspec)
_cspec.loader.exec_module(zc)

POLL = 1800
TRIAL_HOURS = 72
SLEEP_AFTER_END = 6 * 3600
TP = swap.TP_BASE
TRIAL_ACCT = "zcode-hub-trial"
TRIAL_NODES = ["clash-401-teleapi-trial-01", "clash-402-teleapi-trial-02"]
PIN_ACCOUNT = "zcodereg3-dengyietech"          # 固定账号，保证信号一致性
PIGEONW = ["clash-%d-pigeonw-%02d" % (300 + i, i) for i in range(1, 21)]
RESULTS = "/personal/tebi-zcode-hub/zcode-teleapi-trial-results.jsonl"
STATE = "/personal/tebi-zcode-hub/zcode-teleapi-trial-state.json"

GW = ("http://" + TRIAL_ACCT + "__type_clash_proxy__region_us-west__tier_standard"
      "__service_zcode-hub:tetherpool@127.0.0.1:9101")
GW_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": GW, "https": GW}))


def gw_http(url, method="GET", body=None, headers=None, timeout=60):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    t0 = time.time()
    try:
        r = GW_OPENER.open(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def pinned_account():
    con = swap.sqlite3.connect(swap.DB, uri=True)
    try:
        row = con.execute(
            "SELECT data FROM accounts WHERE name=? LIMIT 1",
            (PIN_ACCOUNT,)).fetchone()
    finally:
        con.close()
    return json.loads(row[0])


def billing_headers(acc):
    fp = acc["fingerprint"]
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
        "Authorization": "Bearer " + acc["jwt_token"],
    }


def pin_trial_node(target):
    """exclude_node 排 pigeonw + 另一 trial 节点 → 唯一候选即目标节点。"""
    other = [n for n in TRIAL_NODES if n != target]
    q = urllib.parse.urlencode({
        "service": "zcode-hub", "account_id": TRIAL_ACCT, "region": "us-west",
        "type": "clash_proxy", "region_strict": "true", "avoid_banned": "true",
        "exclude_node": ",".join(PIGEONW + other),
        "force_rebalance": "true"})
    st, raw = swap.http(TP + "/v1/allocation/acquire?" + q, "POST", None,
                        swap.tp_headers(), timeout=20)
    try:
        d = json.loads(raw)
    except Exception:
        d = {}
    node = d.get("node_name")
    return st, node, d.get("exit_ip")


def cycle(state):
    start = state.get("start_epoch")
    if not start:
        state["start_epoch"] = time.time()
        save_state(state)
        swap.log("INFO", "trial 起测（72h 窗口，30min/拍，%s）" % " / ".join(TRIAL_NODES))
        return
    elapsed_h = (time.time() - start) / 3600
    if elapsed_h > TRIAL_HOURS:
        if not state.get("ended_logged"):
            swap.log("INFO", "⏹ 72h 实测窗口已满（%.1fh），进入静默收尾——汇总见 JSONL，待评估" % elapsed_h)
            state["ended_logged"] = True
            save_state(state)
        else:
            swap.log("INFO", "trial 已满窗（静默中，每 %dh 检查一次）" % (SLEEP_AFTER_END // 3600))
        return

    idx = state.get("cycle", 0) % len(TRIAL_NODES)
    target = TRIAL_NODES[idx]
    st, node, exit_ip = pin_trial_node(target)
    if st != 200 or node != target:
        swap.log("WARN", "trial 绑定未钉住 %s（HTTP %s got %s）——本轮跳过" % (target, st, node))
        state["cycle"] = state.get("cycle", 0) + 1
        save_state(state)
        return

    acc = pinned_account()
    rec = {"ts": swap.bj_str(), "node": node, "exit": exit_ip}

    st1, raw1, lat1 = gw_http(
        "http://ip-api.com/json/?fields=query,isp,mobile,proxy", timeout=40)
    rec["exit_check"] = {"http": st1, "body": raw1.strip()[:120]}
    exit_now = exit_ip
    try:
        exit_now = json.loads(raw1).get("query", exit_ip)
    except Exception:
        pass
    rec["exit_seen"] = exit_now

    st2, raw2, lat2 = gw_http(
        "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages", "POST",
        json.dumps({"model": "GLM-5.3-Flash", "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hi"}]}),
        {"Authorization": "Bearer " + acc["jwt_token"],
         "Content-Type": "application/json",
         "User-Agent": "ZCode/3.17.2"}, timeout=60)
    low = raw2.lstrip().lower()
    kind = "HTML-EDGE" if (low.startswith("<!doctype") or "<html" in low[:200]) else "JSON-APP"
    code2 = ""
    try:
        code2 = json.loads(raw2).get("code", "")
    except Exception:
        pass
    rec["messages"] = {"http": st2, "kind": kind, "code": code2, "latency_s": round(lat2, 1)}

    st3, raw3, lat3 = gw_http(
        "https://zcode.z.ai/api/v1/zcode-plan/billing/balance", "GET", None,
        billing_headers(acc), timeout=60)
    code3 = ""
    try:
        code3 = json.loads(raw3).get("code", "")
    except Exception:
        pass
    rec["billing"] = {"http": st3, "code": code3, "latency_s": round(lat3, 1),
                      "body_head": raw3.strip()[:100]}

    ok_msg = kind == "JSON-APP"
    ok_bill = st3 == 200 and code3 == 0
    verdict = "PASS" if (ok_msg and ok_bill) else "WATCH"
    rec["verdict"] = verdict
    swap.log("%s" % ("INFO" if verdict == "PASS" else "WARN"),
             "[TRIAL %d] %s exit=%s messages=%s/%s(%s) billing=%s/%s lat=%.1f/%.1fs → %s"
             % (state.get("cycle", 0) + 1, node, exit_now, st2, kind, code2,
                st3, code3, lat2, lat3, verdict))
    with open(RESULTS, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    state["cycle"] = state.get("cycle", 0) + 1
    save_state(state)


def main():
    swap.log("INFO", "zcode-teleapi-trial 启动（POLL=%ss，窗口 %dh，账号 %s）"
             % (POLL, TRIAL_HOURS, PIN_ACCOUNT))
    state = load_state()
    while True:
        try:
            cycle(state)
        except Exception:
            swap.log("ERROR", "cycle 异常（下一轮重试）:\n" + traceback.format_exc())
        time.sleep(SLEEP_AFTER_END if state.get("ended_logged") else POLL)


if __name__ == "__main__":
    main()
