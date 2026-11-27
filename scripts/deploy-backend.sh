#!/bin/bash
# 后端发版：同步 app/ 代码到 tebi 并重启服务（supervisor 优雅停机，stopwaitsecs=300）
# 用法: scripts/deploy-backend.sh
#
# ⚠️ 生产主机 2026-11-13 起 = tebi（pxed 冻结为回滚件，勿再部署）。
# 2026-11-25 去 pxed 跳板单点（pxed 容器半死事故）：L1 mesh 直连优先（Mac 本身
# 在 mesh .100），不通再按候选列表降级跳板（mesh-tencent → pxed）。
# 同步走 tar 管道（repo app/ → tebi /data/zcode-hub/app/app，嵌套映射），
# 部署前自动备份到 /data/zcode-hub/backups/<ts>/，.old 目录保留上一版可回滚。
set -euo pipefail
HOST=tebi
SRC="$(cd "$(dirname "$0")/.." && pwd)/app"
DST_APP=/data/zcode-hub/app
TS=$(date +%Y%m%d_%H%M%S)

# 选路：L1 直连优先，失败按候选跳板逐个试（去单 VM 依赖，勿再写死单一跳板）
pick_route() {
  if ssh -o ConnectTimeout=8 -o BatchMode=yes "$HOST" true 2>/dev/null; then
    echo "__direct__"; return
  fi
  for j in mesh-tencent pxed; do
    if ssh -o ConnectTimeout=8 -o BatchMode=yes "$j" true 2>/dev/null; then
      echo "$j"; return
    fi
  done
  echo "" # 全挂，由调用方报错
}

[ -d "$SRC" ] || { echo "✗ 找不到 $SRC"; exit 1; }

JUMP="$(pick_route)"
[ -n "$JUMP" ] || { echo "✗ tebi 与全部跳板候选(mesh-tencent/pxed)均不可达"; exit 1; }
if [ "$JUMP" = "__direct__" ]; then
  echo "→ 路由: mesh 直连 ${HOST}（L1）"
  R() { ssh "$HOST" "$@"; }
else
  echo "→ 路由: 经跳板 $JUMP → ${HOST}（L1 直连不可用，已降级）"
  R() { ssh "$JUMP" "ssh $HOST \"$@\""; }
fi

echo "→ 打包同步 app/ → $HOST:$DST_APP/app（备份=/data/zcode-hub/backups/$TS/）"
tar czf - --exclude='__pycache__' --exclude='data' --exclude='statics' -C "$(dirname "$SRC")" app \
  | R "cat > /tmp/zcode-hub-app-$TS.tgz"

R "
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
"

echo "→ 重启 zcode-hub（优雅停机，在途流最长等 300s）"
R "supervisorctl restart zcode-hub"
sleep 3
echo "→ 冒烟 /meta"
R "curl -s --noproxy 127.0.0.1 http://127.0.0.1:8790/meta"
echo
echo "✓ backend → $HOST:$DST_APP/app + 已重启（回滚: $DST_APP/app.old → app + restart；备份 /data/zcode-hub/backups/$TS/）"
