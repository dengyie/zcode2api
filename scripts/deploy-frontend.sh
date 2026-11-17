#!/bin/bash
# 前端独立发版：只同步 frontend/ 到 tebi，零后端重启、不断流
# 用法: scripts/deploy-frontend.sh   （记得先 bump frontend/version 并刷新
#       HTML 里的 ?v= 缓存参数，或改用 deploy_frontend.py 一键做这两步）
#
# ⚠️ 生产主机 2026-11-13 起 = tebi（经 pxed 跳板；pxed 冻结为回滚件，勿再部署）。
# 2026-11-18 修复（线上实测 frontend 发布失败）：
#   1) 原脚本第一跳用本地 $$ 拼 tgz 名、第二跳用远端 $$ 还原——libssh 展开 PID
#      不同，远端 tar 永远找不到包（Cannot open ...tgz）。改为本地一次性算定
#      TMP，两跳内插同一字面量。
#   2) 原脚本把包解到 /data/zcode-hub（解出 frontend/），却去 mv frontend.new
#      —— .new 根本没生成，swap 必失败。改为解到 frontend.new（--strip-
#      components=1）+ 原子换目录，与 deploy-backend.sh 同口径。
set -euo pipefail
JUMP=pxed
HOST=tebi
SRC="$(cd "$(dirname "$0")/.." && pwd)/frontend"
DST=/data/zcode-hub/frontend
TS=$(date +%Y%m%d_%H%M%S)
TMP="/tmp/zcode-hub-fe-$TS-$$.tgz"  # $$ 只在本机展开并内插进两条 ssh——并发发版不撞名

[ -d "$SRC" ] || { echo "✗ 找不到 $SRC"; exit 1; }

# 预检：HTML 里的 ?v= 必须与 frontend/version 一致。否则 CF/浏览器继续用旧
# CSS/JS，线上会复现「HTML 新 + JS 旧」（staggerCards is not defined 那类故障）。
VER="$(cat "$SRC/version" 2>/dev/null || true)"
if [ -n "$VER" ] && ! grep -q "?v=$VER" "$SRC/admin/login.html"; then
  echo "✗ frontend/version=$VER 但 HTML 的 ?v= 未刷新——先跑 deploy_frontend.py（自动 bump+刷新）"
  exit 1
fi

echo "→ 打包同步 frontend/ → $HOST:${DST}（回滚: ${DST}.old 换回）"
tar czf - --exclude='.DS_Store' -C "$(dirname "$SRC")" frontend \
  | ssh "$JUMP" "ssh $HOST 'cat > $TMP'"

ssh "$JUMP" "ssh $HOST \"
  set -e
  rm -rf $DST.new
  mkdir -p $DST.new
  tar xzf $TMP -C $DST.new --strip-components=1
  rm -rf $DST.old
  mv $DST $DST.old
  mv $DST.new $DST || { mv $DST.old $DST; echo '✗ 换目录失败，已按 .old 回滚' >&2; exit 1; }
  rm -f $TMP
\""

echo "→ 冒烟（源站直连 127.0.0.1 绕开 tebi 的 http_proxy 劫持；-f 让非 2xx 直接判失败）"
ssh "$JUMP" "ssh $HOST 'echo 版本=\$(cat $DST/version); curl -sf --noproxy 127.0.0.1 -o /dev/null -w \"accounts.html HTTP %{http_code}\\n\" http://127.0.0.1:8790/admin/accounts.html'"

echo "✓ frontend → $HOST:${DST}（静态文件从磁盘热读，无需重启；回滚: ${DST}.old）"
