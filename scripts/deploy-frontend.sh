#!/bin/bash
# 前端独立发版：只同步 frontend/ 到 pxed，零后端重启、不断流
# 用法: scripts/deploy-frontend.sh   （记得先 bump frontend/version 以刷新浏览器缓存）
set -euo pipefail
HOST=pxed
SRC="$(cd "$(dirname "$0")/.." && pwd)/frontend"
DST=/data/zcode-hub/frontend

rsync -az --delete "$SRC/" "$HOST:$DST/"
echo "✓ frontend → $HOST:$DST （静态文件从磁盘热读，无需重启）"
echo "  验证: curl -s https://zcode.mangoqwq.com/admin/login | grep -o 'v=[0-9.]*' | head -1"
