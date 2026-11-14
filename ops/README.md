# ops/ · 线上运维 agent（tebi 常驻，supervisor 托管）

本目录是**部署在 tebi `/personal/tebi-zcode-hub/` 的常驻运维脚本的版本化正本**
（NFS 持久、独立于 hub 进程生命周期）。改这里 → scp 同步到 tebi → 对应
supervisor 程序 restart。权威运维手册：Obsidian vault `Note/Infra/pxed zcode-hub 部署与运维`。

## zcode-egress-swap.py · 边缘封禁自动换出口

- **触发**：轮询 accounts.db（只读 ro）`recent_results` 的「边缘封禁」detail
  （epoch 水位防重放；JSON 3012「unusual activity」不触发——2.7.5 口径属请求形状不符）
- **动作**：TetherPool `POST /v1/feedback/ban`（隔离被封出口）→
  `acquire(force_rebalance, avoid_banned, avoid_same_isp, avoid_cidr)` →
  按编号一一对应钉 mihomo 组（`clash-(300+NN)-pigeonw-NN` ↔ `[自建] 🇺🇸PIGEONW-家宽-NN`）
  → 直连探针（零 strike）+ hub 端到端双验证 → 新出口仍封自动二轮
- **防抖**：冷却 600s / 每日（北京日）6 次；状态文件 `zcode-egress-swap-state.json`（NFS 持久）
- **降级**：TetherPool 不可达 / 池枯 → 保持现状 + ALERT（绝不本地降级直连机房 IP）
- **配置**：token 文件 `/personal/tebi-zcode-hub/tetherpool_token`（0600，凭据页
  `Note/Accounts/网络账号/TetherPool 控制面凭据`）；mihomo 控制器 `/personal/clash/.controller-secret`
- **`DRY_RUN=1`**：全写操作门禁（ban/acquire/PUT/状态落盘都只打印不执行）
- supervisor：`/etc/supervisor/conf.d/zcode-egress-swap.conf`；日志 `/personal/clash/zcode-egress-swap.log`

## zcode-teleapi-trial.py · teleapi(Bright Data) 出口 3 天风控实测（需求单 §11-2）

- **节奏**：30min/拍，trial-01/02 轮换（`exclude_node` 排 pigeonw + 另一 trial 节点 → 确定性钉住），
  经 `:9101` 网关独立绑定账号 `zcode-hub-trial` 走 teleapi 出口
- **双平面探针**：messages（JSON 3012=边缘放行形状预期值 / HTML-EDGE=封）+
  billing（保真指纹头，200=风控接受；429/3007/unusual=拒）
- **结果**：`/personal/tebi-zcode-hub/zcode-teleapi-trial-results.jsonl`（逐拍一行）；
  72h 后自动静默收尾，汇总供扩池决策
- supervisor：`/etc/supervisor/conf.d/zcode-teleapi-trial.conf`；日志 `/personal/clash/zcode-teleapi-trial.log`

## 同步 SOP

```bash
scp ops/zcode-egress-swap.py pxed:/tmp/ && ssh pxed "scp -q /tmp/zcode-egress-swap.py tebi:/tmp/" \
  && ssh pxed "ssh tebi 'install -m 644 /tmp/zcode-egress-swap.py /personal/tebi-zcode-hub/'"
ssh pxed "ssh tebi 'supervisorctl restart zcode-egress-swap'"   # 仅脚本有行为级变更时
```
（token 文件由 agent 每次调用现读，无需重启；hub 重启与两个 agent 互不影响）
