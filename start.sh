#!/usr/bin/env bash
# zcode-hub Linux 启动脚本（对应 Windows 的 start.bat）
#
# 用法:
#   ./start.sh          启动（若已在运行则先停旧实例再启动，同时清除后台登录失败锁定）
#   ./start.sh stop     停止
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# ── 可按需修改 ──────────────────────────────────────────────
# 上游代理（Clash 混合端口）；直连环境把它留空即可
PROXY="http://127.0.0.1:7890"
# ────────────────────────────────────────────────────────────

PORT=$(grep -E '^ZCODE_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
PORT=${PORT:-3000}

stop_instance() {
    local old_pid
    old_pid=$(cat serve.pid 2>/dev/null || true)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        echo "[*] 停止旧实例 (pid=$old_pid)..."
        kill "$old_pid" 2>/dev/null || true
        for _ in $(seq 1 10); do
            kill -0 "$old_pid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$old_pid" 2>/dev/null || true
    fi
    rm -f serve.pid
}

if [ "${1:-}" = "stop" ]; then
    stop_instance
    echo "[✔] 已停止"
    exit 0
fi

# 1. Python 虚拟环境：缺失则创建，依赖缺失则安装
if [ ! -x .venv/bin/python ]; then
    echo "[*] 创建虚拟环境 (.venv)..."
    python3 -m venv .venv
fi
if ! .venv/bin/python -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
    echo "[*] 安装 Python 依赖..."
    if [ -n "$PROXY" ]; then
        .venv/bin/pip install -q --proxy "$PROXY" -r requirements.txt
    else
        .venv/bin/pip install -q -r requirements.txt
    fi
fi

# 2. 验证码求解 Node 依赖（JWT 账号需要）
if [ ! -d captcha_node/node_modules ]; then
    echo "[*] 安装 captcha_node 依赖 (npm install)..."
    (cd captcha_node && npm install --no-audit --no-fund)
fi

# 3. 已在运行则先停（重启同时清掉内存里的后台登录失败锁定）
stop_instance

# 4. 端口仍被其他进程占用则报错
if ss -tln 2>/dev/null | grep -q ":${PORT} "; then
    echo "[!] 端口 ${PORT} 仍被其他进程占用，请手动处理：" >&2
    ss -tlnp 2>/dev/null | grep ":${PORT} " >&2 || true
    exit 1
fi

# 5. 启动（不设 ALL_PROXY/socks —— httpx 缺 socks 支持会报 Unknown scheme）
echo "[*] 启动 zcode-hub (port=${PORT})..."
if [ -n "$PROXY" ]; then
    env "HTTP_PROXY=${PROXY}/" "HTTPS_PROXY=${PROXY}/" \
        nohup .venv/bin/python cli.py serve > serve.log 2>&1 &
else
    nohup .venv/bin/python cli.py serve > serve.log 2>&1 &
fi
echo $! > serve.pid

# 6. 健康检查
for _ in $(seq 1 15); do
    sleep 1
    if curl -sf -m 2 "http://127.0.0.1:${PORT}/meta" >/dev/null 2>&1; then
        echo "[✔] 已启动: http://127.0.0.1:${PORT}"
        echo "    后台:  http://127.0.0.1:${PORT}/admin/login"
        echo "    日志:  serve.log    停止: ./start.sh stop"
        exit 0
    fi
done

echo "[!] 启动失败，最近日志：" >&2
tail -20 serve.log >&2
exit 1
