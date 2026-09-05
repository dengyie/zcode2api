#!/bin/bash
# 后端发版：同步 app/ 代码并重启服务（会中断在途请求，stopwaitsecs=300 优雅停机）
# 用法: scripts/deploy-backend.sh
set -euo pipefail
HOST=pxed
SRC="$(cd "$(dirname "$0")/.." && pwd)/app"
DST=/data/zcode-hub/app/app

# 前端目录已独立（frontend/ → /data/zcode-hub/frontend），不再随 app/ 同步
rsync -az --delete --exclude '__pycache__' --exclude 'data' \
  --exclude 'statics' "$SRC/" "$HOST:$DST/"
ssh "$HOST" "supervisorctl -c /personal/pxed/supervisord.conf restart zcode-hub"
echo "✓ backend → $HOST:$DST + 已重启"
ssh "$HOST" "sleep 2 && curl -s --noproxy '*' http://127.0.0.1:8790/meta"
echo
