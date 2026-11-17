#!/usr/bin/env bash
# opslog 排障查询工具（2.7.64）——tebi 上一键过滤 zcode-hub 运维事件日志。
#
# 用法（在 tebi 上执行；OPSLOG_FILE 缺省 = 线上数据目录）：
#   opslog-grep.sh                          # 尾部 50 行
#   opslog-grep.sh -t captcha               # 只看 captcha 模块
#   opslog-grep.sh -t claim -l warn         # claim 的全部告警
#   opslog-grep.sh -d 2026-11-18 -k DEGRADED  # 指定日期 + 关键词
#   opslog-grep.sh -n 200                   # 尾部 200 行
#   opslog-grep.sh -f                       # 跟随模式（tail -f）
#
# 输出为人类可读行（_ts [LEVEL] tag msg），亦可用 --raw 输出原始 JSON。
set -euo pipefail

OPSLOG_FILE="${OPSLOG_FILE:-/data/zcode-hub/data/opslog.jsonl}"
TAG="" LEVEL="" KW="" DATE="" N=50 FOLLOW=0 RAW=0
while getopts "t:l:k:d:n:fr-" opt; do
  case "$opt" in
    t) TAG="$OPTARG" ;;
    l) LEVEL="$OPTARG" ;;
    k) KW="$OPTARG" ;;
    d) DATE="$OPTARG" ;;
    n) N="$OPTARG" ;;
    f) FOLLOW=1 ;;
    *) RAW=1 ;;
  esac
done
shift $((OPTIND - 1))
[ $# -gt 0 ] && KW="$KW $*"

[ -f "$OPSLOG_FILE" ] || { echo "opslog 不存在: $OPSLOG_FILE" >&2; exit 1; }

jq_filter='. as $e'
[ -n "$DATE" ]  && jq_filter="$jq_filter | select(($e._ts | startswith(\"$DATE\")))"
[ -n "$TAG" ]   && jq_filter="$jq_filter | select($e.tag == \"$TAG\")"
[ -n "$LEVEL" ] && jq_filter="$jq_filter | select($e.level == \"$LEVEL\")"
[ -n "$KW" ]    && jq_filter="$jq_filter | select(($e.msg | ascii_downcase | contains(\"$(echo "$KW" | tr '[:upper:]' '[:lower:]')\")))"

if [ "$RAW" = 1 ]; then
  if [ "$FOLLOW" = 1 ]; then tail -f "$OPSLOG_FILE" | jq -c "$jq_filter"; else jq -c "$jq_filter" "$OPSLOG_FILE" | tail -n "$N"; fi
else
  fmt='"\($e._ts) [\($e.level | ascii_upcase)] \($e.tag) \($e.msg)"'
  if [ "$FOLLOW" = 1 ]; then tail -f "$OPSLOG_FILE" | jq -r "$jq_filter | $fmt"; else jq -r "$jq_filter | $fmt" "$OPSLOG_FILE" | tail -n "$N"; fi
fi
