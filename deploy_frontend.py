#!/usr/bin/env python3
"""zcode-hub 前端一键发版：bump version → 统一 ?v= 缓存参数 → tar/scp tebi → 远端解压。

用法：python deploy_frontend.py [--skip-push]
版本参数 ?v= 自动同步 frontend/version，杜绝手动维护漂移。
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
VER_FILE = ROOT / "frontend" / "version"


def sh(cmd, check=True):
    print("+", cmd)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0 and check:
        sys.exit(f"FAILED: {cmd}\n{r.stderr}")
    return r.stdout.strip()


def main():
    old = VER_FILE.read_text(encoding="utf-8").strip()
    parts = old.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    new = ".".join(parts)
    VER_FILE.write_text(new, encoding="utf-8")
    print(f"version {old} -> {new}")

    # 统一本地引用缓存参数（css/vendor/js 三类本地资源）
    n = 0
    for rel in (ROOT / "frontend" / "admin").rglob("*.html"):
        t = rel.read_text(encoding="utf-8")
        # 先刷新已有 ?v=，再给尚无参数的引用打标（两步都写回）
        t = re.sub(r'(href="css/app\.css)\?v=[\w.]+"', r"\1?v=" + new + '"', t)
        t = re.sub(r'(src="(?:vendor|js)/[A-Za-z0-9_]+\.js)\?v=[\w.]+"', r"\1?v=" + new + '"', t)
        t = re.sub(r'(href="css/app\.css)"', r"\1?v=" + new + '"', t)
        t = re.sub(r'(src="(?:vendor|js)/[A-Za-z0-9_]+\.js)"', r"\1?v=" + new + '"', t)
        if "?v=" + new in t:
            n += t.count("?v=" + new)
            rel.write_text(t, encoding="utf-8")
    print(f"cache-bust refs -> ?v={new}: {n}")

    sh("git add frontend/ && git commit -m 'chore(frontend): release " + new + "' || true", check=False)
    sh("git push origin main", check=False)
    sh("tar -czf /tmp/zchub-frontend.tar.gz frontend")
    sh("scp -q /tmp/zchub-frontend.tar.gz tebi:/tmp/")
    sh('ssh tebi "cd /data/zcode-hub && tar -xzf /tmp/zchub-frontend.tar.gz && rm /tmp/zchub-frontend.tar.gz && cat frontend/version"')
    print("deployed")


if __name__ == "__main__":
    main()
