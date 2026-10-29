#!/usr/bin/env bash
# zcode-hub Linux 启动脚本（极简版 + 代理兜底：前台运行、关终端即停）
cd "$(dirname "$(readlink -f "$0")")"

# 代理兜底（对齐 e3222ef）：Clash 系统代理可能注入 socks:// 形式的 ALL_PROXY，
# httpx 不支持该 scheme，会导致全部上游请求 500。仅在检测到非 http(s) 值时剔除，
# 正常环境原样启动。
for v in ALL_PROXY all_proxy; do
    val="${!v:-}"
    case "$val" in
        ""|http://*|https://*) ;;
        *) unset "$v"; echo "[i] 已剔除 $v=$val（httpx 不支持该代理 scheme）" ;;
    esac
done

echo "Starting zcode-hub on http://127.0.0.1:3000 ..."
.venv/bin/python cli.py serve
