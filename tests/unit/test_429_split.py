"""429 业务码分流 + 边缘封禁判定的分类器单测（2026-11 magpie 实证扩展）。

覆盖 gateway 三个纯函数级判定：并发族（3008/3009/3010）、真限流（1302/1303）、
边缘封 IP 页面（405 + 地址文案，非 3012）。所有判定只认解析后的业务码——
logid 20 位随机数字，裸子串必撞车（test_regression_baseline 的同款教训）。
"""

from __future__ import annotations

from app.routes.gateway import _is_concurrency_hit, _is_edge_block, _is_rate_limit_hit


class TestConcurrencyFamily:
    def test_3009_business_code(self):
        body = '{"code":3009,"msg":"model concurrency limit exceeded","logid":"202611030003009"}'
        assert _is_concurrency_hit(body) is True

    def test_3008_and_3010_same_family(self):
        """3008（账号并发）/3010（轮次并发）与 3009 同族同处置（magpie 实证）。"""
        assert _is_concurrency_hit('{"code":3008,"msg":"concurrency limit exceeded"}') is True
        assert _is_concurrency_hit('{"code":3010,"msg":"round concurrency limit exceeded"}') is True

    def test_text_fallback_for_non_json(self):
        assert _is_concurrency_hit("model concurrency limit exceeded") is True

    def test_logid_substring_alone_is_not_enough(self):
        body = '{"code":1302,"msg":"rate limited","logid":"202611030003009xxxx"}'
        assert _is_concurrency_hit(body) is False

    def test_other_codes_are_not_concurrency(self):
        assert _is_concurrency_hit('{"code":1113,"msg":"insufficient balance"}') is False
        assert _is_concurrency_hit("server busy") is False


class TestRateLimitFamily:
    def test_1302_and_1303_are_rate_limit(self):
        assert _is_rate_limit_hit('{"code":1302,"msg":"too many requests"}') is True
        assert _is_rate_limit_hit('{"code":1303,"msg":"rate limit exceeded"}') is True

    def test_concurrency_codes_are_not_rate_limit(self):
        """并发族与真限流互斥：并发不休息，限流不重试。"""
        assert _is_rate_limit_hit('{"code":3009,"msg":"model concurrency limit exceeded"}') is False
        assert _is_rate_limit_hit("rate limit exceeded") is False  # 非业务码不认

    def test_plain_text_is_not_rate_limit(self):
        assert _is_rate_limit_hit("plain 429 body") is False


class TestEdgeBlock:
    def test_address_blocked_page(self):
        """magpie 实证形态：405 + 「network's address was blocked」＝ IP 级封禁。"""
        assert _is_edge_block(405, '{"msg":"Your network\'s address was blocked by the edge."}') is True
        assert _is_edge_block(405, "<html>address was blocked</html>") is True

    def test_3012_is_not_edge(self):
        """3012 承载在 405 上：账号级风控，走指数退避，不是 IP 封禁。"""
        body = '{"code":3012,"msg":"request has been blocked due to unusual activity."}'
        assert _is_edge_block(405, body) is False

    def test_other_status_is_not_edge(self):
        assert _is_edge_block(403, "address was blocked") is False

    def test_html_page_on_405_is_edge(self):
        """宽口径（Magpie EDGE_PAGE 同款）：405 + 任何 HTML 页 = 边缘页，
        WAF 改版不换本质；裸 405 / JSON 3012 仍归账号风控。"""
        assert _is_edge_block(405, "<html><body>oops</body></html>") is True
        assert _is_edge_block(405, "redirect to errors.aliyun.com") is True

    def test_unrelated_405_is_not_edge(self):
        assert _is_edge_block(405, '{"msg":"method not allowed"}') is False
