"""Provider 命名空间识别测试（Magpie 模式）。

- `_detect_provider` 对模型名前缀 (`zai/`, `bigmodel/`) 与 `x-provider` header 的识别
- `/v1/models` 返回带前缀的模型目录
- 裸模型名向后兼容（默认 zai）
"""

from __future__ import annotations

import pytest

from app.routes.gateway import _detect_provider


class TestDetectProviderNamespacing:
    def test_bare_model_defaults_to_zai(self):
        assert _detect_provider({"model": "GLM-5.3"}, {}) == "zai"

    def test_zai_prefix_detected(self):
        assert _detect_provider({"model": "zai/GLM-5.3"}, {}) == "zai"
        assert _detect_provider({"model": "zai/GLM-5.3-Flash"}, {}) == "zai"

    def test_bigmodel_prefix_detected(self):
        assert _detect_provider({"model": "bigmodel/GLM-5.3"}, {}) == "bigmodel"
        assert _detect_provider({"model": "bigmodel/GLM-5.3-Flash"}, {}) == "bigmodel"

    def test_x_provider_header(self):
        assert _detect_provider({}, {"x-provider": "bigmodel"}) == "bigmodel"
        assert _detect_provider({}, {"x-provider": "zai"}) == "zai"

    def test_model_prefix_priority_over_header(self):
        # 模型名前缀优先于 x-provider header
        assert _detect_provider(
            {"model": "zai/GLM-5.3"}, {"x-provider": "bigmodel"},
        ) == "zai"
        assert _detect_provider(
            {"model": "bigmodel/GLM-5.3"}, {"x-provider": "zai"},
        ) == "bigmodel"

    def test_unknown_prefix_falls_back_to_zai(self):
        # 不认识的前缀 → 默认 zai（向后兼容裸模型名）
        assert _detect_provider({"model": "something/GLM-5.3"}, {}) == "zai"
        assert _detect_provider({"model": "openai/gpt-4"}, {}) == "zai"

    def test_empty_model(self):
        assert _detect_provider({}, {}) == "zai"


@pytest.mark.integration
class TestModelsEndpointNamespacing:
    async def test_models_endpoint_returns_prefixed_ids(self, gateway_client):
        """/v1/models 返回 Magpie 命名空间前缀 `zai/` 和 `bigmodel/`。"""
        client, _ = gateway_client
        res = await client.get("/v1/models")
        assert res.status_code == 200
        payload = res.json()
        assert payload["object"] == "list"
        ids = [m["id"] for m in payload["data"]]
        assert "zai/GLM-5.3" in ids
        assert "zai/GLM-5.3-Flash" in ids
        assert "bigmodel/GLM-5.3" in ids
        assert "bigmodel/GLM-5.3-Flash" in ids
        # 裸名已移除（仅前缀形式）
        assert "GLM-5.3" not in ids

    async def test_models_each_has_required_fields(self, gateway_client):
        client, _ = gateway_client
        res = await client.get("/v1/models")
        for m in res.json()["data"]:
            assert "id" in m
            assert "type" in m
            assert m["type"] == "model"
            assert "display_name" in m
