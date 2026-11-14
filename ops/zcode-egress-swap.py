#!/usr/bin/env python3
"""zcode-egress-swap · zcode-hub 出口封禁自动换 IP（TetherPool 消费端 agent）。

模式无关（2026-11-14 立项，需求单见 vault Note/project/ip-pool/）：
  检测到上游边缘 405（IP 级）封禁 → TetherPool ban(当前节点) + acquire(force_rebalance,
  avoid_banned, avoid_same_isp) → 若 hub 仍在 mihomo 路径：按编号一一对应钉 mihomo 组
  （clash-(300+NN)-pigeonw-NN ↔ [自建] 🇺🇸PIGEONW-家宽-NN）；割接 :9101 网关后
  mihomo 钉自动退化为 no-op，TetherPool 绑定即数据面。

检测源：accounts.db（只读 ro）recent_results 的「边缘封禁」detail（带 epoch 时间戳，
权威信号；JSON 3012「unusual activity」不算——2.7.5 口径属请求形状不符）。
防抖：冷却 600s；每日（北京日）上限 6 次；超限 ALERT。
降级：TetherPool 不可达 / pool_exhausted → 保持现状 + ALERT（绝不本地降级直连机房 IP，
绝不禁用账号）。
Token：/personal/tebi-zcode-hub/tetherpool_token（0600，缺省=不带头，tethepool 设
IP_POOL_API_TOKEN 后放入即生效）。
DRY_RUN=1：只打印将执行的动作，不做任何写操作（含状态文件）。
"""

import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

TP_BASE = "http://127.0.0.1:9100"          # 云端控制面（tebi port_forward）
GATEWAY_HINT = "127.0.0.1:9101"            # 割接后 hub HTTPS_PROXY 指向（仅日志提示用）
MIHOMO = "http://127.0.0.1:9090"
MIHOMO_SECRET_FILE = "/personal/clash/.controller-secret"
GRP = "zcode-us-residential"
DB = "file:/data/zcode-hub/data/accounts.db?mode=ro"
PLAN_URL = "https://zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages"
HUB = "http://127.0.0.1:8790/v1/messages"
EGRESS_PROXY = "http://127.0.0.1:11030"
STATE_FILE = "/personal/tebi-zcode-hub/zcode-egress-swap-state.json"
TOKEN_FILE = "/personal/tebi-zcode-hub/tetherpool_token"

SERVICE = "zcode-hub"
ACCOUNT = "zcode-hub-global"
COOLDOWN = 600          # 换 IP 冷却（秒）
DAILY_LIMIT = 6         # 每日上限（北京日）
GATE_HOLD = 180         # hub edge_hold 闸门时长
POLL = 60               # 检测轮询间隔

NOPROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": EGRESS_PROXY, "https": EGRESS_PROXY}))


def now_bj():
    return time.gmtime(time.time() + 8 * 3600)


def bj_str(epoch=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime((epoch or time.time()) + 8 * 3600))


def bj_date(epoch=None):
    return time.strftime("%Y-%m-%d", time.gmtime((epoch or time.time()) + 8 * 3600))


def log(level, msg):
    print("%s [%s] %s" % (bj_str(), level, msg), flush=True)


def dry():
    return os.environ.get("DRY_RUN") == "1"


def http(url, method="GET", body=None, headers=None, timeout=15, via_proxy=False,
         _opener=None):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if _opener is not None:
        opener = _opener
    else:
        opener = PROXY_OPENER if via_proxy else NOPROXY_OPENER
    try:
        r = opener.open(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def tp_headers():
    h = {"Content-Type": "application/json"}
    try:
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            h["Authorization"] = "Bearer " + tok
    except OSError:
        pass
    return h


def mihomo_api(method, path, payload=None):
    secret = open(MIHOMO_SECRET_FILE).read().strip()
    body = json.dumps(payload).encode() if payload is not None else None
    st, raw = http(MIHOMO + path, method, body,
                   {"Authorization": "Bearer " + secret}, timeout=10)
    try:
        return st, json.loads(raw)
    except Exception:
        return st, {"_raw": raw[:200]}


def mihomo_node_number(name):
    """[自建] 🇺🇸PIGEONW-家宽-06 → 6；非 pigeonw 命名返回 None。"""
    if "PIGEONW" in name and "家宽-" in name:
        try:
            return int(name.rsplit("家宽-", 1)[1].strip())
        except ValueError:
            return None
    return None


def tp_name_from_mihomo(name):
    nn = mihomo_node_number(name)
    if nn is None:
        return None
    return "clash-%d-pigeonw-%02d" % (300 + nn, nn)


def mihomo_name_from_tp(node_name):
    try:
        nn = int(node_name.rsplit("-pigeonw-", 1)[1])
        return "[自建] 🇺🇸PIGEONW-家宽-%02d" % nn
    except (ValueError, IndexError):
        return None


PER_ACCOUNT_PORT_BASE = 11031  # zcode-acc-01..07 → 11031-11037（2.7.36 per-account 隔离）


def account_egress_url(account_id):
    """读 hub DB（ro）拿账号绑定的专属出口 listener URL；未绑定返回 None。"""
    try:
        con = sqlite3.connect(DB, uri=True)
        row = con.execute("SELECT data FROM accounts WHERE id=?",
                          (account_id,)).fetchone()
        con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return json.loads(row[0]).get("egress")


def group_for_listener(egress_url):
    """http://127.0.0.1:11031 → zcode-acc-01（端口即组序号）。"""
    try:
        port = int(str(egress_url).rsplit(":", 1)[1])
        return "zcode-acc-%02d" % (port - PER_ACCOUNT_PORT_BASE + 1), port
    except (ValueError, IndexError):
        return None, None


def _opener(proxy):
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def tp_banned_nodes():
    """TetherPool 上 service=zcode-hub 的在隔离节点名集合（换 IP 时避开）。"""
    try:
        st, raw = http(TP_BASE + "/v1/feedback/bans?service=" + SERVICE,
                       headers=tp_headers(), timeout=10)
        out = set()
        for b in json.loads(raw or "[]"):
            if b.get("released_at"):
                continue
            if b.get("node_name"):
                out.add(b["node_name"])
        return out
    except Exception:
        return set()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    if os.environ.get("DRY_RUN") == "1":
        log("DRY", "（DRY_RUN 跳过状态落盘）")
        return
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def detect(watermark):
    """返回 (命中列表, 新水位)。命中 = recent_results 中含「边缘封禁」且 at > watermark。"""
    con = sqlite3.connect(DB, uri=True)
    try:
        rows = con.execute(
            "SELECT id, name, json_extract(data,'$.recent_results') FROM accounts "
            "WHERE enabled=1").fetchall()
    finally:
        con.close()
    hits, latest = [], watermark
    for acc_id, name, arr in rows:
        try:
            results = json.loads(arr) if arr else []
        except Exception:
            continue
        for r in results:
            at = float(r.get("at") or 0)
            latest = max(latest, at)
            if at > watermark and "边缘封禁" in str(r.get("detail") or ""):
                hits.append({"at": at, "account": name, "account_id": acc_id,
                             "detail": str(r.get("detail"))[:90]})
    hits.sort(key=lambda h: h["at"])
    return hits, latest


def _retry_after_seconds(body):
    """从 hub 闸门报错文案解析剩余秒数（「…请 20s 后重试」）。"""
    mm = re.search(r"请\s*(\d+)\s*s", body or "")
    return int(mm.group(1)) if mm else None


def _cidr24(ip):
    """IPv4 → /24 网段（非 IPv4 返回 None）。"""
    parts = str(ip).split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(parts[:3]) + ".0/24"
    return None


def egress_ip(proxy=PROXY):
    st, raw = http("http://ip-api.com/json/?fields=query,isp",
                   via_proxy=True, timeout=12, _opener=_opener(proxy))
    try:
        d = json.loads(raw)
        return d.get("query", "?"), d.get("isp", "?")
    except Exception:
        return "?", raw.strip()[:60]


def direct_probe(jwt, proxy=PROXY):
    """零 strike 直连探针：JSON-APP=边缘放行（3012 属形状预期值），HTML-EDGE=仍封。

    proxy 可指定账号专属 listener（per-account 模式逐账号验证）。"""
    st, raw = http(PLAN_URL, "POST",
                   json.dumps({"model": "GLM-5.3-Flash", "max_tokens": 16,
                               "messages": [{"role": "user", "content": "hi"}]}),
                   {"Authorization": "Bearer " + jwt,
                    "Content-Type": "application/json",
                    "User-Agent": "ZCode/3.17.2"},
                   timeout=30, via_proxy=True, _opener=_opener(proxy))
    low = raw.lstrip().lower()
    kind = "HTML-EDGE" if (low.startswith("<!doctype") or "<html" in low[:200]
                           or "<title>405" in low) else "JSON-APP"
    return st, kind, raw.strip()[:140]


def hub_probe(key):
    """hub 端到端 200 验证（edge_hold 闸门期会 503，由调用方重试）。"""
    st, raw = http(HUB, "POST",
                   json.dumps({"model": "GLM-5.3-Flash", "max_tokens": 16,
                               "messages": [{"role": "user", "content": "只回复 pong"}]}),
                   {"x-api-key": key, "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"}, timeout=90)
    try:
        d = json.loads(raw)
        text = "".join(c.get("text", "") for c in d.get("content", []))
        gate = "edge_hold" in json.dumps(d, ensure_ascii=False)
    except Exception:
        text, gate = "", False
    return st, text[:30], gate, raw.strip()[:160]


def get_db_secrets():
    con = sqlite3.connect(DB, uri=True)
    try:
        row = con.execute(
            "SELECT data FROM accounts WHERE status='active' AND enabled=1 "
            "ORDER BY json_extract(data,'$.last_used_at') DESC LIMIT 1").fetchone()
        jwt = json.loads(row[0])["jwt_token"]
        key = con.execute(
            "SELECT value FROM meta WHERE key='gateway_key'").fetchone()[0]
    finally:
        con.close()
    return jwt, key


def swap(state, hit):
    """一次完整换 IP 流程。返回 True=已切换成功。

    per-account 出口隔离（2.7.36）：命中账号绑定了专属 listener（Account.egress）
    时走按账号换 IP（只动该账号的 zcode-acc-NN 组）；未绑定走共享组全池换 IP
    （兼容模式，行为与 2.7.35 一致）。
    """
    egress_url = account_egress_url(hit.get("account_id") or "")
    if egress_url:
        return _swap_one_account(state, hit, egress_url)
    return _swap_shared(state, hit)


def _swap_one_account(state, hit, egress_url):
    """per-account：只换命中账号绑定 listener 的出口节点（其余账号零影响）。"""
    group, port = group_for_listener(egress_url)
    if not group:
        log("ALERT", f"账号 {hit['account']} 的 egress {egress_url} 无法解析组名——人工介入")
        return False
    jwt, key = get_db_secrets()
    ip_old, isp_old = egress_ip(f"http://127.0.0.1:{port}")
    _, g = mihomo_api("GET", "/proxies/" + group)
    cur_node = g.get("now", "") if isinstance(g, dict) else ""
    cur_tp = tp_name_from_mihomo(cur_node)
    log("INFO", "[per-account] 触发: %s（account=%s）；组 %s 当前 %s 出口 %s(%s)"
        % (hit["detail"], hit["account"], group, cur_node, ip_old, isp_old))
    if not cur_tp:
        log("ALERT", f"组 {group} 当前节点 {cur_node} 非 pigeonw 命名——人工介入")
        return False

    # 1) ban 该账号当前节点（TetherPool 隔离：全池不再分配该 IP）
    if dry():
        log("DRY", f"（DRY_RUN 将执行: ban {cur_tp} + 换 {group} 出口节点 + 端口 {port} 探针）")
        return False
    st, resp = http(TP_BASE + "/v1/feedback/ban", "POST",
                    json.dumps({"service": SERVICE, "account": ACCOUNT,
                                "node_name": cur_tp, "reason": "edge_405_html",
                                "evidence": {"http_status": 405, "body_kind": "html",
                                             "observed_at": bj_str(hit["at"]) + "+08:00",
                                             "hub_detail": hit["detail"],
                                             "account": hit["account"]}}),
                    tp_headers(), timeout=15)
    log("INFO", f"ban({cur_tp}) → HTTP {st} {str(resp)[:150]}")

    # 2) 选新节点：排除其它账号组当前钉的节点 + TP 在隔离节点 + 当前节点
    banned = tp_banned_nodes()
    taken = set()
    for i in range(1, 8):
        gn = "zcode-acc-%02d" % i
        if gn == group:
            continue
        _, og = mihomo_api("GET", "/proxies/" + gn)
        if isinstance(og, dict) and og.get("now"):
            taken.add(og["now"])
    free = [n for n in (g.get("all") or [])
            if n != cur_node and n not in taken and tp_name_from_mihomo(n) not in banned]
    if not free:
        log("ALERT", f"[per-account] {group} 无可用新节点（其余被占用/隔离）——保持现状人工介入")
        return False
    target = free[0]

    st, resp = mihomo_api("PUT", "/proxies/" + group, {"name": target})
    if st not in (200, 204):
        log("ALERT", f"PUT {group} → {target} 失败 HTTP {st}")
        return False
    time.sleep(2)
    g2 = mihomo_api("GET", "/proxies/" + group)[1]
    if g2.get("now") != target:
        log("ALERT", f"{group} 未按预期选中 {target}（now={g2.get('now')}）")
        return False
    ip_new, isp_new = egress_ip(f"http://127.0.0.1:{port}")
    log("INFO", f"[per-account] 已钉 {target}，出口 {ip_old} → {ip_new}({isp_new})")
    if ip_new == ip_old:
        log("WARN", "出口 IP 未变化（同线/未生效）——由直连探针把关")

    # 3) 直连探针（走该账号自己的 listener，零 strike）
    st, kind, body = direct_probe(jwt, proxy=f"http://127.0.0.1:{port}")
    log("INFO", f"直连探针(端口 {port}): HTTP {st} {kind} {body}")
    if kind != "JSON-APP":
        log("ALERT", f"[per-account] 新节点 {target} 仍被边缘封——已 ban，待下轮再换")
        http(TP_BASE + "/v1/feedback/ban", "POST",
             json.dumps({"service": SERVICE, "account": ACCOUNT,
                         "node_name": tp_name_from_mihomo(target),
                         "reason": "edge_405_html",
                         "evidence": {"http_status": st, "body_kind": "html",
                                      "probe": "per_account_post_swap"}}),
             tp_headers(), timeout=15)
        return False

    # 4) hub 端到端（时间预算制，覆盖 edge_hold 闸门全窗）
    deadline = time.time() + GATE_HOLD + 45
    st, text, gate, body = None, "", False, ""
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        st, text, gate, body = hub_probe(key)
        log("INFO", "hub端到端(第%d次): HTTP %s reply=%s gate_hold=%s" % (attempt, st, text, gate))
        if st == 200:
            log("INFO", "✅ [per-account] %s 出口已换：%s → %s（%s → %s），流量恢复"
                % (hit["account"], ip_old, ip_new, cur_tp, tp_name_from_mihomo(target)))
            return True
        if gate or st == 503:
            wait = _retry_after_seconds(body) or 45
            wait = max(5, min(wait + 2, 75))
            log("INFO", "hub edge_hold 闸门未过期（剩约 %ss），%ds 后重试" % (wait, wait))
            time.sleep(wait)
        else:
            break
    log("ALERT", "hub 端到端未达 200（最后 HTTP %s %s）——直连探针已放行、出口已换，人工复核"
        % (st, body[:120]))
    return False


def _swap_shared(state, hit):
    """共享出口模式（兼容 2.7.35 行为）：全池换共享组的节点。"""
    _, cur = mihomo_api("GET", "/proxies/" + GRP)
    cur_name = cur.get("now", "")
    cur_nn = mihomo_node_number(cur_name)
    cur_tp = tp_name_from_mihomo(cur_name)
    ip_old, isp_old = egress_ip()
    log("INFO", "触发: %s（account=%s）；当前 mihomo=%s（TP=%s）出口=%s(%s)"
        % (hit["detail"], hit["account"], cur_name, cur_tp, ip_old, isp_old))
    if cur_tp is None:
        log("ALERT", "当前 mihomo 组选中的不是 pigeonw 家宽节点（%s），无法映射 TP 节点名——人工介入" % cur_name)
        return False

    jwt, key = get_db_secrets()
    avoid_cidrs = [c for c in [_cidr24(ip_old)] if c]

    # 1) ban 当前节点（真实封禁，不 release）
    if dry():
        log("DRY", "（DRY_RUN 将执行: ban %s + acquire(force_rebalance,avoid_banned,avoid_same_isp)，随后双探针验证）" % cur_tp)
        return False
    st, resp = http(TP_BASE + "/v1/feedback/ban", "POST",
                    json.dumps({"service": SERVICE, "account": ACCOUNT,
                                "node_name": cur_tp, "reason": "edge_405_html",
                                "evidence": {"http_status": 405, "body_kind": "html",
                                             "observed_at": bj_str(hit["at"]) + "+08:00",
                                             "hub_detail": hit["detail"]}}),
                    tp_headers(), timeout=15)
    log("INFO", "ban(%s) → HTTP %s %s" % (cur_tp, st, str(resp)[:180]))
    if st not in (200, 201, 202):
        log("ALERT", "ban 上报失败（HTTP %s）——继续尝试 acquire（隔离可能未生效，人工核对）" % st)

    # 2) acquire 新节点（最多 2 轮：新节点若也被边缘封，再 ban 再换）
    new_tp = None
    for attempt in (1, 2):
        qd = {"service": SERVICE, "account_id": ACCOUNT, "region": "us-west",
              "type": "clash_proxy", "region_strict": "true",
              "avoid_banned": "true", "avoid_same_isp": "true",
              "force_rebalance": "true"}
        if avoid_cidrs:
            qd["avoid_cidr"] = ",".join(avoid_cidrs)
        q = urllib.parse.urlencode(qd)
        st, raw = http(TP_BASE + "/v1/allocation/acquire?" + q, "POST", None,
                       tp_headers(), timeout=20)
        if st != 200:
            log("ALERT", "acquire 失败（HTTP %s）%s——保持现状（TetherPool 不可用/池枯，绝不本地降级）"
                % (st, str(raw)[:150]))
            return False
        d = json.loads(raw)
        cand = d.get("node_name")
        log("INFO", "acquire(第%d轮) → %s exit=%s isp=%s warning=%s"
            % (attempt, cand, d.get("exit_ip"), d.get("isp"), d.get("warning")))
        if d.get("warning"):
            log("WARN", "TetherPool 警告: %s" % d.get("warning"))
        if cand == cur_tp:
            log("ALERT", "acquire 返回与当前相同的节点 %s（avoid 失效？）——不动，人工核对" % cand)
            return False
        # 3) mihomo 钉新节点（模式无关：割接 9101 后此步自然 no-op/失败降级）
        target = mihomo_name_from_tp(cand)
        if target is None:
            log("ALERT", "TP 节点名 %s 无法映射回 mihomo 命名——人工介入" % cand)
            return False
        if os.environ.get("DRY_RUN") == "1":
            log("DRY", "（DRY_RUN 将执行: PUT %s -> %s，随后双探针验证）" % (GRP, target))
            return False
        st, resp = mihomo_api("PUT", "/proxies/" + GRP, {"name": target})
        if st not in (200, 204):
            log("WARN", "mihomo 钉节点失败（HTTP %s %s）——若已割接 :9101 属预期，绑定已随 acquire 切换"
                % (st, str(resp)[:120]))
            return True  # 网关模式下绑定已切换
        time.sleep(3)
        g2 = mihomo_api("GET", "/proxies/" + GRP)[1]
        if g2.get("now") != target:
            log("ALERT", "mihomo 未按预期选中 %s（now=%s）" % (target, g2.get("now")))
            return False
        ip_new, isp_new = egress_ip()
        log("INFO", "已钉 %s，出口 %s(%s) → %s(%s)" % (target, ip_old, isp_old, ip_new, isp_new))
        if ip_new == ip_old:
            log("WARN", "出口 IP 未变化（同线/未生效）——继续用该节点验证，异常由探针把关")
        # 4) 双探针：直连（零 strike）→ hub 端到端（闸门感知重试）
        st, kind, body = direct_probe(jwt)
        log("INFO", "直连探针: HTTP %s %s %s" % (st, kind, body))
        if kind == "HTML-EDGE":
            log("WARN", "新节点 %s 仍被边缘封——ban 后换下一轮" % cand)
            http(TP_BASE + "/v1/feedback/ban", "POST",
                 json.dumps({"service": SERVICE, "account": ACCOUNT,
                             "node_name": cand, "reason": "edge_405_html",
                             "evidence": {"http_status": st, "body_kind": "html",
                                          "probe": "post_swap_direct"}}),
                 tp_headers(), timeout=15)
            c2 = _cidr24(d.get("exit_ip") or "")
            if c2 and c2 not in avoid_cidrs:
                avoid_cidrs.append(c2)
            cur_tp, cur_name = cand, target
            continue
        new_tp = cand
        break

    if new_tp is None:
        log("ALERT", "两轮 acquire 均拿到被边缘封禁的出口——保持最后节点并告警人工（池可能整段被拉黑）")
        return False

    # 4) hub 端到端：闸门感知（时间预算制——预算覆盖整个 edge_hold 窗口 + 余量，
    #    首版固定 3 次重试在闸门尾部差 ~20s 导致假 ALERT，06:48 实战教训）
    deadline = time.time() + GATE_HOLD + 45
    st, text, gate, body = None, "", False, ""
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        st, text, gate, body = hub_probe(key)
        log("INFO", "hub端到端(第%d次): HTTP %s reply=%s gate_hold=%s" % (attempt, st, text, gate))
        if st == 200:
            log("INFO", "✅ 换 IP 完成：%s → %s（TP 绑定=%s），流量恢复" % (ip_old, ip_new, new_tp))
            return True
        if gate or st == 503:
            wait = _retry_after_seconds(body) or 45
            wait = max(5, min(wait + 2, 75))
            log("INFO", "hub edge_hold 闸门未过期（剩约 %ss），%ds 后重试" % (wait, wait))
            time.sleep(wait)
        else:
            break
    log("ALERT", "hub 端到端未达 200（最后 HTTP %s %s）——直连探针已确认边缘放行、出口已换；"
                 "若属 gate 503 验证窗口不足，流量在闸门过期后自愈，人工复核 %s"
        % (st, body[:120], GATEWAY_HINT))
    return False


def cycle(state):
    hits, latest = detect(state.get("watermark", 0))
    if not state.get("watermark"):
        log("INFO", "首次运行：水位初始化为 %.0f（不回溯历史封禁）" % latest)
        state["watermark"] = latest
        save_state(state)
        return
    if hits:
        log("INFO", "检测到 %d 条新边缘封禁（最新 %s %s）" % (len(hits), bj_str(hits[-1]["at"]), hits[-1]["detail"]))
    state["watermark"] = latest
    if not hits:
        save_state(state)
        return
    now = time.time()
    if now - state.get("last_swap_epoch", 0) < COOLDOWN:
        log("INFO", "冷却窗内（上次换 IP %s），跳过本次触发" % bj_str(state.get("last_swap_epoch")))
        save_state(state)
        return
    day = bj_date()
    if state.get("swap_day") != day:
        state["swap_day"], state["swap_count"] = day, 0
    if state.get("swap_count", 0) >= DAILY_LIMIT:
        log("ALERT", "已达每日换 IP 上限 %d 次——保持现状，人工介入" % DAILY_LIMIT)
        save_state(state)
        return
    ok = swap(state, hits[-1])
    state["last_swap_epoch"] = now
    state["swap_count"] = state.get("swap_count", 0) + 1
    state["last_result"] = ("ok" if ok else "failed") + "@" + bj_str()
    save_state(state)


def main():
    log("INFO", "zcode-egress-swap 启动（POLL=%ss COOLDOWN=%ss DAILY=%d DRY_RUN=%s）"
        % (POLL, COOLDOWN, DAILY_LIMIT, os.environ.get("DRY_RUN", "0")))
    state = load_state()
    while True:
        try:
            cycle(state)
        except Exception:
            log("ERROR", "cycle 异常（下一轮重试）:\n" + traceback.format_exc())
        time.sleep(POLL)


if __name__ == "__main__":
    main()
