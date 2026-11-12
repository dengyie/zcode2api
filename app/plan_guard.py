"""Plan 通道模型级额度耗尽冷却（2.7.29）。

同一账号的不同模型额度独立（线上实证：GLM-5.3 旗舰上游 1005 exceed quota
limit 时同号 Flash 窗口仍有 2.9 亿）——点名单一模型的请求若不按模型记冷却，
会把全池每个号都撞一遍必然失败的 1005 才 503。

独立成模块的原因：gateway（打点/跳号）与 quota（billing 刷新主动解除）
两侧都要用，而 gateway → quota 已有依赖，quota 反向 import gateway 会成环
——与 app/gate.py「进程级状态独立模块」同一手法。

记忆形态：f"{account_id}\\n{model}" → 解禁 epoch。
- 打点：gateway exhaust 分支（1005/1113/1310，Plan 通道）
- 跳号：_dispatch 选号循环（不计 attempts，同满载跳过）
- 解除：① TTL 到期自然重探；② quota.fetch_quota 实查拿到该号账本时按模型
  定向清除（billing 知道**哪些窗口可用**——同号 Flash 有余量不等于旗舰恢复了，
  整号清除会把旗舰冷却反复抹掉，重探退化成 1 次/分钟）；③ 后台启用/凭证变更
  （admin set_enabled 调 clear_account 整号清除，人工启用 = 要求立即重试）
进程内记忆：重启即忘，重探一次代价可接受。
"""

from __future__ import annotations

import time

# TTL 由 settings 注入（避免 settings ↔ 本模块的加载顺序问题），见
# settings.PLAN_MODEL_EXHAUST_COOLDOWN 的调用方传参。
_plan_model_exhausted: dict[str, float] = {}


def _key(account_id: str, model: str) -> str:
    return f"{account_id}\n{model}"


def note(account_id: str, model: str, until: float) -> None:
    """记录（账号，模型）额度耗尽冷却的解禁时刻。"""
    if not account_id or not model:
        return
    _plan_model_exhausted[_key(account_id, model)] = until


def cooling(account_id: str, model: str, now: float | None = None) -> bool:
    """该（账号，模型）组合是否仍在冷却中；到期自动摘除（自然重探）。"""
    if not account_id or not model:
        return False
    until = _plan_model_exhausted.get(_key(account_id, model), 0)
    if not until:
        return False
    if (now or time.time()) >= until:
        _plan_model_exhausted.pop(_key(account_id, model), None)
        return False
    return True


def clear_model(account_id: str, model: str) -> None:
    """billing 实查确认该（账号，模型）有可用窗口时定向清除。

    粒度必须对齐打点：同号其它模型的冷却不连坐清除（它们是否耗尽仍未知，
    清了只会重演一次必然失败的 1005）。
    """
    _plan_model_exhausted.pop(_key(account_id, model), None)


def clear_account(account_id: str) -> None:
    """整号清除：后台人工启用（要求立即重试）/凭证变更时用。

    billing 刷新**不要**走这里——billing 只能证明「有窗口可用」的模型恢复了，
    证明不了其它模型没耗尽（2.7.30 review 整改）。
    """
    for k in [k for k in _plan_model_exhausted if k.split("\n", 1)[0] == account_id]:
        _plan_model_exhausted.pop(k, None)
