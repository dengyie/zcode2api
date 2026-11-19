#!/bin/bash
# zcode-hub 告警守护（2.7.71）：进程失联 + opslog 关键故障模式 → TG @mangoqwqbot
# 模式沿用 zcode-egress-guard-loop.sh / inspect-daemon 门控（每键 6h 去重）
set -uo pipefail
DIR=/root/.zcode-hub-alert
ENVF=$DIR/.env.tg
STATE=$DIR/alert-state.json
HUB=http://127.0.0.1:8790
OPSLOG=/data/zcode-hub/data/opslog.jsonl
COOLDOWN=21600   # 每键 6h 去重
INTERVAL=300
TG_TOKEN=$(grep INSPECT_TG_TOKEN= "$ENVF" | cut -d= -f2-)
TG_CHAT=$(grep INSPECT_TG_CHAT_ID= "$ENVF" | cut -d= -f2-)

tg_send() {  # $1=text
  local text="$1"
  for P in 10822 11030; do
    R=$(curl -sm 20 -x socks5h://127.0.0.1:$P -o /tmp/zha_tg.json -w '%{http_code}' \
      "https://api.telegram.org/bot$TG_TOKEN/sendMessage" \
      --data-urlencode "chat_id=$TG_CHAT" --data-urlencode "text=$text" 2>/dev/null)
    [ "$R" = "200" ] && return 0
    sleep 2
  done
  echo "$(date '+%F %T') TG send failed" >> "$DIR/alert-err.log"
  return 1
}

cooldown_ok() {  # $1=key —— JSON 状态文件按键去重
  [ -f "$STATE" ] || echo '{}' > "$STATE"
  local now=$(date +%s) last
  last=$(python3 -c "
import json,sys
try: print(json.load(open('$STATE')).get('$1', 0))
except Exception: print(0)")
  [ $((now - last)) -ge $COOLDOWN ] || return 1
  python3 - "$STATE" "$1" "$now" <<'PY'
import json, sys, os
p, k, now = sys.argv[1], sys.argv[2], int(sys.argv[3])
try: d = json.load(open(p))
except Exception: d = {}
d[k] = now
json.dump(d, open(p, "w"))
PY
  return 0
}

alert() {  # $1=key $2=text
  cooldown_ok "$1" && tg_send "$2" && echo "$(date '+%F %T') ALERT sent: $1" >> "$DIR/alert-sent.log"
}

LAST_TS=""
DOWN=0
echo "$(date '+%F %T') zcode-hub alert loop started (interval ${INTERVAL}s)"
while true; do
  # ① 进程存活探测：连续 2 轮失败才告警；恢复报平安
  M=$(curl -sm 8 "$HUB/meta" 2>/dev/null)
  if [ -z "$M" ]; then
    DOWN=$((DOWN + 1))
    [ $DOWN -ge 2 ] && alert "hub_down" "🔴 zcode-hub 进程失联（连续 $DOWN 轮 /meta 无响应，tebi $(date '+%T')）"
  else
    [ $DOWN -ge 2 ] && tg_send "🟢 zcode-hub 进程恢复（$M）" >/dev/null
    DOWN=0
  fi

  # ② opslog 关键故障模式（自上次处理点起，单次最多扫 3000 行防重复劳动）
  if [ -f "$OPSLOG" ]; then
    CUR_LAST=$(cat "$DIR/.last_ts" 2>/dev/null || echo "")
    if [ -n "$CUR_LAST" ]; then
      SCAN=$(tail -c 2000000 "$OPSLOG" | python3 -c "
import json, sys
last = '$CUR_LAST'
out = []
for line in sys.stdin:
    try: x = json.loads(line)
    except Exception: continue
    if x.get('_ts','') > last:
        out.append(x)
for x in out[-3000:]:
    print(json.dumps(x, ensure_ascii=False))
")
      echo "$SCAN" | python3 -c "
import json, sys
mx = ''
for line in sys.stdin:
    try: x = json.loads(line)
    except Exception: continue
    mx = max(mx, x.get('_ts',''))
    m = x.get('msg','')
    if '人工介入' in m:
        print('rebalance_manual\x01' + x['_ts'] + ' ' + m[:150])
    elif '升级禁用（第' in m:
        print('risk_ban\x01' + x['_ts'] + ' ' + m[:150])
    elif 'listener 探针连续' in m and '冷却' in m:
        print('listener_dead\x01' + x['_ts'] + ' ' + m[:150])
    elif '本轮预算耗尽' in m or '轮级预算' in m and '耗尽' in m:
        print('claim_budget\x01' + x['_ts'] + ' ' + m[:150])
    elif m.startswith('<!>') and '无可用账号' in m:
        print('pool_exhausted\x01' + x['_ts'] + ' ' + m[:150])
if mx: print('LASTTS\x01' + mx)
" > "$DIR/.alerts.out"
      NEW_LAST=""
      while IFS= read -r line; do
        key=${line%%$'\x01'*}
        body=${line#*$'\x01'}
        if [ "$key" = "LASTTS" ]; then NEW_LAST="$body"; continue; fi
        [ -z "$body" ] && continue
        alert "hub_$key" "🚨 zcode-hub [$key]
$body"
      done < "$DIR/.alerts.out"
      [ -n "$NEW_LAST" ] && echo -n "$NEW_LAST" > "$DIR/.last_ts"
    else
      # 首轮只记录水位，不回扫历史
      T=$(tail -c 2000000 "$OPSLOG" | python3 -c "
import json, sys
mx = ''
for line in sys.stdin:
    try: x = json.loads(line)
    except Exception: continue
    mx = max(mx, x.get('_ts',''))
print(mx)")
      echo -n "$T" > "$DIR/.last_ts"
    fi
  fi
  sleep "$INTERVAL"
done
