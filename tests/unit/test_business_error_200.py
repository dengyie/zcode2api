"""200 承载业务错误的判定与映射（gateway 内部小函数）单元矩阵。

判定必须**只**认「无 message 形状 + code 非 0/200」：任何带 type/content 的
响应都算正常响应，否则会把合法 message 误杀成错误（比原缺陷更糟）。
"""

from __future__ import annotations

import pytest

from app.routes.gateway import _business_error_code, _status_for_business_code


class TestBusinessErrorCode:
    @pytest.mark.parametrize("data,expected", [
        ({"code": 1005, "msg": "exceed quota limit"}, 1005),
        ({"code": 3012, "msg": "request has been blocked due to unusual activity."}, 3012),
        ({"code": 3006, "msg": "model not allowed"}, 3006),
        ({"code": 0, "msg": "ok"}, None),
        ({"code": 200}, None),
        ({"msg": "no code"}, None),
        ({"code": "1005"}, None),          # 字符串码不采信
        ({"code": True}, None),            # bool 是 int 子类，必须排除
        ({}, None),
        # 带 message 形状 → 一律不算业务错误（正常响应）
        ({"type": "message", "code": 1005}, None),
        ({"content": [], "code": 1005}, None),
    ])
    def test_matrix(self, data, expected):
        assert _business_error_code(data) == expected


class TestStatusForBusinessCode:
    @pytest.mark.parametrize("code,expected", [
        (3012, 405),   # 风控走 RISK_CONTROL_HTTP_STATUSES
        (1005, 402),   # 额度走 EXHAUST_HTTP_STATUSES
        (1113, 402),
        (3006, 400),   # 其余业务拒绝按 400 原样回传
        (3007, 400),
    ])
    def test_matrix(self, code, expected):
        assert _status_for_business_code(code) == expected


class TestConstantsCoverage:
    """映射出来的状态码必须落在既有分支常量里，否则分支不会命中。"""

    def test_risk_status_is_recognized(self):
        from app import constants
        assert _status_for_business_code(3012) in constants.RISK_CONTROL_HTTP_STATUSES

    def test_exhaust_status_is_recognized(self):
        from app import constants
        assert _status_for_business_code(1005) in constants.EXHAUST_HTTP_STATUSES
