"""上游「HTTP 200 承载业务错误」的统一识别。

2026-10-30/31 线上实证（Plan 通道）：额度与风控类拒绝经常**不是** 4xx，而是
HTTP 200 + `application/json` + `{"code":1005,"msg":"exceed quota limit"}`。
不识别有三个后果：

1. messages：错误体当正文透传给客户端（「200 但没有 message」），成功分支还会
   洗掉 risk_strikes / 冷却 / last_error，把仍在标记期的号拉回 ACTIVE；
2. billing 全家桶：业务失败被当成空数据写库，把 quota 快照 / plans 洗空；
3. 告警与指标全绿，问题只能靠人肉盯。

判定口径（唯一一处）：合法响应一定带业务形状（messages 有 `type`/`content`，
billing 有 `data` 或 `code` 为 0/200）；因此「无业务形状 + code 是非 0/200 的
整数」即业务错误。带 type/content 的一律不判错，避免误杀正常响应。
"""

from __future__ import annotations


def business_code(payload: object) -> int | None:
    """返回 HTTP 200 响应里承载的业务错误码；不是业务错误则 None。"""
    if not isinstance(payload, dict):
        return None
    if "type" in payload or "content" in payload:
        return None  # messages 正常响应形状
    code = payload.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return None if code in (0, 200) else code


def is_quota_error(code: int | None) -> bool:
    """业务码是否属于「额度/余额耗尽」一类。"""
    from . import constants

    return code is not None and code in constants.EXHAUST_BUSINESS_CODES
