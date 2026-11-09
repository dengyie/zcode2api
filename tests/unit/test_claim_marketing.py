"""marketing/touch 活动套餐发现与解析单元测试（2026-11-03 官方逆向实证）。"""

from __future__ import annotations

from app.claim import extract_plans_from_marketing_deliveries


def test_extract_plans_from_marketing_banner_zcode_plan():
    deliveries = [
        {
            "campaign_id": "2101141259813175296",
            "resource_position": "banner",
            "priority": 50,
            "banner": {
                "buttons": [
                    {"text": {"format": "", "content": ""}, "action": {"type": "close", "args": None}},
                    {"text": {"format": "", "content": ""}, "action": {"type": "claim_zcode_plan", "args": {"plan_id": "zcode-v3-start-plan-1103"}}},
                ],
                "args": {
                    "zcode_plan": {
                        "plan_id": "zcode-v3-start-plan-1103",
                        "name": "ZCODE GLOBAL BUILD",
                        "priority": 111,
                        "starts_at": 1793703600,
                        "ends_at": 1793754000,
                        "entitlements": [
                            {
                                "entitlement_id": "ent-1",
                                "show_name": "GLM-5.3-Flash",
                                "meter": "model_usage",
                                "unit_type": "token",
                                "grant_units": 300000000,
                                "period": "one_time",
                                "effective_at": 1793718000,
                                "priority": 101,
                            }
                        ],
                    }
                },
            },
        }
    ]
    plans = extract_plans_from_marketing_deliveries(deliveries)
    assert len(plans) == 1
    p = plans[0]
    assert p["plan_id"] == "zcode-v3-start-plan-1103"
    assert p["name"] == "ZCODE GLOBAL BUILD"
    assert p["priority"] == 111
    assert len(p["grants"]) == 1
    assert p["grants"][0]["name"] == "GLM-5.3-Flash"
    assert p["grants"][0]["units"] == 300_000_000.0


def test_extract_plans_from_button_fallback():
    # 当 args 中未携带完整 zcode_plan 元数据时，仍能从 button.action 中发现 plan_id
    deliveries = [
        {
            "campaign_id": "c2",
            "banner": {
                "buttons": [
                    {"action": {"type": "claim_zcode_plan", "args": {"plan_id": "zcode-v3-start-plan-1104"}}}
                ]
            },
        }
    ]
    plans = extract_plans_from_marketing_deliveries(deliveries)
    assert len(plans) == 1
    assert plans[0]["plan_id"] == "zcode-v3-start-plan-1104"
    assert plans[0]["name"] == "zcode-v3-start-plan-1104"


def test_extract_plans_deduplicates_and_ignores_noise():
    deliveries = [
        {
            "campaign_id": "c1",
            "banner": {
                "buttons": [
                    {"action": {"type": "claim_zcode_plan", "args": {"plan_id": "p1"}}},
                    {"action": {"type": "other_action", "args": {"foo": "bar"}}},
                ],
                "args": {
                    "zcode_plan": {
                        "plan_id": "p1",
                        "name": "Plan 1",
                        "entitlements": [
                            {"show_name": "GLM-5.3", "meter": "model_usage", "unit_type": "token", "grant_units": 100}
                        ],
                    }
                },
            },
        },
        {
            "campaign_id": "c2",
            "popup": {
                "buttons": [
                    {"action": {"type": "claim_zcode_plan", "args": {"plan_id": "p1"}}},
                ]
            },
        },
    ]
    plans = extract_plans_from_marketing_deliveries(deliveries)
    assert len(plans) == 1
    assert plans[0]["plan_id"] == "p1"
    assert plans[0]["name"] == "Plan 1"
    assert plans[0]["grants"][0]["units"] == 100


def test_extract_plans_empty_or_malformed():
    assert extract_plans_from_marketing_deliveries([]) == []
    assert extract_plans_from_marketing_deliveries(None) == []  # type: ignore[arg-type]
    assert extract_plans_from_marketing_deliveries([{"noise": 123}]) == []
