#!/bin/bash
# 后端发版：同步 app/ 代码到 tebi 并重启服务（supervisor 优雅停机，stopwaitsecs=300）
# 用法: scripts/deploy-backend.sh
#
# ⚠️ 生产主机 2026-11-13 起 = tebi（经 pxed 跳板；pxed 冻结为回滚件，勿再部署）。
# 同步走 tar 管道（repo app/ → tebi /data/zcode-hub/app/app，嵌套映射），
# 部署前自动备份到 /data/zcode-hub/backups/<ts>/，.old 目录保留上一版可回滚。
set -euo pipefail
JUMP=pxed
HOST=tebi
SRC="$(cd "$(dirname "$0")/.." && pwd)/app"
DST_APP=/data/zcode-hub/app
TS=$(date +%Y%m%d_%H%M%S)

[ -d "$SRC" ] || { echo "✗ 找不到 $SRC"; exit 1; }

echo "→ 打包同步 app/ → $HOST:$DST_APP/app（备份=/data/zcode-hub/backups/$TS/）"
tar czf - --exclude='__pycache__' --exclude='data' --exclude='statics' -C "$(dirname "$SRC")" app \
  | ssh "$JUMP" "ssh $HOST 'cat > /tmp/zcode-hub-app-$TS.tgz'"

ssh "$JUMP" "ssh $HOST \"
  set -e
  mkdir -p /data/zcode-hub/backups/$TS
  cp -a $DST_APP/app/. /data/zcode-hub/backups/$TS/
  rm -rf $DST_APP/app.new
  mkdir -p $DST_APP/app.new
  tar xzf /tmp/zcode-hub-app-$TS.tgz -C $DST_APP/app.new --strip-components=1
  rm -rf $DST_APP/app.old
  mv $DST_APP/app $DST_APP/app.old
  mv $DST_APP/app.new $DST_APP/app
  rm -f /tmp/zcode-hub-app-$TS.tgz
\""

echo "→ 重启 zcode-hub（优雅停机，在途流最长等 300s）"
ssh "$JUMP" "ssh $HOST 'supervisorctl restart zcode-hub'"
sleep 3
echo "→ 冒烟 /meta"
ssh "$JUMP" "ssh $HOST 'curl -s --noproxy 127.0.0.1 http://127.0.0.1:8790/meta'"
echo
echo "✓ backend → $HOST:$DST_APP/app + 已重启（回滚: $DST_APP/app.old → app + restart；备份 /data/zcode-hub/backups/$TS/）"
