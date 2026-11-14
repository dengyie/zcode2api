#!/bin/bash
# 前端独立发版：只同步 frontend/ 到 tebi，零后端重启、不断流
# 用法: scripts/deploy-frontend.sh   （记得先 bump frontend/version 以刷新浏览器缓存）
#
# ⚠️ 生产主机 2026-11-13 起 = tebi（经 pxed 跳板；pxed 冻结为回滚件，勿再部署）。
set -euo pipefail
JUMP=pxed
HOST=tebi
SRC="$(cd "$(dirname "$0")/.." && pwd)/frontend"
DST=/data/zcode-hub/frontend

[ -d "$SRC" ] || { echo "✗ 找不到 $SRC"; exit 1; }

tar czf - -C "$(dirname "$SRC")" frontend \
  | ssh "$JUMP" "ssh $HOST 'cat > /tmp/zcode-hub-fe-$$.tgz'"

ssh "$JUMP" "ssh $HOST \"
  set -e
  rm -rf $DST.new
  tar xzf /tmp/zcode-hub-fe-\$\$.tgz -C /data/zcode-hub
  mv $DST $DST.old && mv $DST.new $DST
  rm -f /tmp/zcode-hub-fe-\$\$.tgz
\""

echo "✓ frontend → $HOST:$DST （静态文件从磁盘热读，无需重启）"
echo "  验证: curl -s https://zcode.mangoqwq.com/admin/login | grep -o 'v=[0-9.]*' | head -1"
