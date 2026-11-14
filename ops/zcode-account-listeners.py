#!/usr/bin/env python3
"""zcode-account-listeners · tebi mihomo 加 7 个按账号固定的出口 listener。

per-account 出口隔离（2.7.36）的 mihomo 侧手术：group `zcode-acc-01..07`
（成员 = 全部 PIGEONW 家宽节点，type: select）+ listener `127.0.0.1:11031-11037`
（proxy → 对应 group）。hub 侧每账号绑定一个 listener URL（Account.egress），
验证码/闸门/调度全部按它隔离。

SOP（对齐 hutao-bridge-guard 先例，见 vault「tebi zashboard 项目开发」§48）：
  ClashConfigLock 全程持锁 + atomic_write_text 双配置文件（config.yaml +
  config.mac-merged.yaml）+ 命名备份（keeper 不清 .bak-<名字> 形态）+
  `mihomo -t` 双校验 + `PUT /configs?force=true` 热载（零重启）。
幂等：已存在的 group/listener 跳过。DRY_RUN=1 只打印不动文件。

用法（tebi 上）: /data/zcode-hub/venv/bin/python zcode-account-listeners.py [--check]
"""

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/personal/clash")
from clash_lock import ClashConfigLock, atomic_write_text  # noqa: E402

import yaml  # noqa: E402

CONFIGS = ["/personal/clash/config.yaml", "/personal/clash/config.mac-merged.yaml"]
SECRET_FILE = "/personal/clash/.controller-secret"
API = "http://127.0.0.1:9090"
BASE_PORT = 11031
GROUP_FMT = "zcode-acc-{nn:02d}"
COUNT = 7
DRY = __import__("os").environ.get("DRY_RUN") == "1"


def log(msg):
    print(time.strftime("%m-%d %H:%M:%S"), msg, flush=True)


def api(method, path, payload=None):
    secret = open(SECRET_FILE).read().strip()
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + secret,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def port_in_use(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def pigeonw_names(cfg):
    return [p["name"] for p in cfg.get("proxies", [])
            if "PIGEONW" in str(p.get("name", "")) and "家宽" in str(p.get("name", ""))]


def mihomo_bin():
    found = shutil.which("mihomo")
    if found:
        return found
    for cand in ("/usr/local/bin/mihomo", "/personal/clash/mihomo/mihomo",
                 "/personal/clash/mihomo"):
        try:
            if (cand and "mihomo" in cand and __import__("os").path.isfile(cand)
                    and __import__("os").access(cand, __import__("os").X_OK)):
                return cand
        except OSError:
            continue
    return None


def main():
    check_only = "--check" in sys.argv
    groups_needed = {GROUP_FMT.format(nn=i + 1): BASE_PORT + i
                     for i in range(COUNT)}

    changed_files = []
    for path in CONFIGS:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        nodes = pigeonw_names(cfg)
        if len(nodes) < COUNT:
            log(f"!! {path}: PIGEONW 节点只有 {len(nodes)} 个（需 ≥{COUNT}），跳过该文件")
            continue
        groups = cfg.setdefault("proxy-groups", [])
        listeners = cfg.setdefault("listeners", [])
        have_groups = {g.get("name") for g in groups}
        have_listeners = {l.get("name") for l in listeners}
        dirty = False
        for name, port in groups_needed.items():
            if name not in have_groups:
                groups.append({"name": name, "type": "select", "proxies": list(nodes)})
                dirty = True
            listener_ports = {l.get("port") for l in listeners}
            if name not in have_listeners and port not in listener_ports:
                listeners.append({"name": name, "type": "mixed", "port": port,
                                  "listen": "127.0.0.1", "proxy": name})
                dirty = True
        if not dirty:
            log(f"= {path}: groups/listeners 已在位，跳过")
            continue
        if check_only or DRY:
            log(f"[DRY] {path}: 将追加 {COUNT} group + {COUNT} listener")
            continue
        backup = path + ".bak-zcode-listeners-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(path, backup)
        with ClashConfigLock():
            atomic_write_text(path, yaml.safe_dump(cfg, allow_unicode=True,
                                                   sort_keys=False, width=100))
        changed_files.append((path, backup))
        log(f"✓ {path} 已写入（备份 {backup}）")

    if check_only or DRY or not changed_files:
        return

    mihomo = mihomo_bin()
    if not mihomo:
        log("!! 找不到 mihomo 二进制——请手动 mihomo -t 校验后再热载")
        sys.exit(2)
    for path, _ in changed_files:
        res = subprocess.run([mihomo, "-t", "-f", path], capture_output=True, text=True)
        ok = "test is successful" in (res.stdout + res.stderr)
        log(("✓" if ok else "!!") + f" mihomo -t {path}: "
             f"{(res.stdout + res.stderr).strip()[-80:]}")
        if not ok:
            log("!! 配置校验失败——回滚：cp <备份> <配置> 后重试")
            sys.exit(2)

    st, body = api("PUT", "/configs?force=true", {"path": CONFIGS[0]})
    log(f"{'✓' if st in (200, 204) else '!!'} 热载 PUT /configs?force=true → HTTP {st} {body[:80]}")
    if st not in (200, 204):
        sys.exit(2)
    time.sleep(2)
    for name, port in sorted(groups_needed.items()):
        if port_in_use(port):
            log(f"? {name} 端口 {port} 已有监听（正常，mihomo 热载后）")
    log("完成。逐 listener 出口验证：")
    for name, port in sorted(groups_needed.items()):
        log(f"  env -u http_proxy -u https_proxy curl -s -m 10 "
            f"-x 127.0.0.1:{port} 'http://ip-api.com/json/?fields=query'  # {name}")


if __name__ == "__main__":
    main()
