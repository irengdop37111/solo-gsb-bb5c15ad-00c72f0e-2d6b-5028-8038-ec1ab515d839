"""判定引擎单元测试：分桶、规则、解释链。"""

import pytest

from app.engine import (
    Rule,
    RuleEvaluationError,
    Variant,
    decide,
    decide_override,
    evaluate_rule,
    pick_variant_by_weight,
    stable_bucket,
)

VARIANTS = [Variant("on", 10), Variant("off", 90)]
RULES = [
    Rule(2, "region", "in", ["cn", "sg"], "on"),
    Rule(1, "employee", "eq", [True], "on"),
]


def test_stable_bucket_is_deterministic_and_in_range():
    values = {stable_bucket("flag", f"user-{i}") for i in range(500)}
    assert values <= set(range(100))
    for _ in range(10):
        assert stable_bucket("flag", "user-42") == stable_bucket("flag", "user-42")


def test_different_keys_differ():
    # 不同 flag_key 通常应产生不同桶（哈希空间足够）
    buckets = {stable_bucket(f"flag-{i}", "user") for i in range(20)}
    assert len(buckets) > 1


def test_pick_variant_respects_ranges():
    assert pick_variant_by_weight(VARIANTS, 0)[0] == "on"
    assert pick_variant_by_weight(VARIANTS, 9)[0] == "on"
    assert pick_variant_by_weight(VARIANTS, 10)[0] == "off"
    assert pick_variant_by_weight(VARIANTS, 99)[0] == "off"


def test_missing_attribute_is_miss():
    hit, reason = evaluate_rule(RULES[1], {})
    assert hit is False
    assert "缺少属性" in reason
    hit, _ = evaluate_rule(RULES[1], {"employee": None})
    assert hit is False


def test_priority_order_first_hit_wins():
    # employee 规则 priority=1 先命中，即便 region 也满足
    d = decide("f", "u1", {"employee": True, "region": "cn"}, VARIANTS, RULES)
    assert d.decision_path == "rule"
    assert d.variant_key == "on"
    assert d.matched_rule.attribute == "employee"
    assert d.bucket_value is None
    assert d.explanation[-1]["step"] == "rule_selected"


def test_lower_priority_rule_can_hit_after_first_misses():
    # employee 属性缺失 -> 第一条未命中；region 命中第二条
    d = decide("f", "u2", {"region": "cn"}, VARIANTS, RULES)
    assert d.decision_path == "rule"
    assert d.matched_rule.attribute == "region"
    assert d.matched_rule_index == 1


def test_fallback_to_bucket_and_explanation():
    d = decide("f", "u3", {"region": "us"}, VARIANTS, RULES)
    assert d.decision_path == "bucket"
    assert d.bucket_value == stable_bucket("f", "u3")
    steps = [e["step"] for e in d.explanation]
    assert steps == [
        "rules_loaded",
        "rule_evaluated",
        "rule_evaluated",
        "no_rule_matched",
        "bucket_computed",
        "variant_selected",
    ]


def test_rule_referencing_unknown_variant_raises():
    bad = [Rule(1, "region", "eq", ["cn"], "ghost")]
    with pytest.raises(RuleEvaluationError):
        decide("f", "u", {"region": "cn"}, VARIANTS, bad)


def test_numeric_and_string_operators():
    assert evaluate_rule(Rule(1, "age", "gte", [18], "on"), {"age": 20})[0] is True
    assert evaluate_rule(Rule(1, "age", "gte", [18], "on"), {"age": "x"})[0] is False
    assert evaluate_rule(Rule(1, "email", "suffix", ["@corp.com"], "on"),
                         {"email": "a@corp.com"})[0] is True
    assert evaluate_rule(Rule(1, "email", "suffix", ["@corp.com"], "on"),
                         {"email": 123})[0] is False


def test_zero_weight_variant_never_bucketed():
    vs = [Variant("a", 0), Variant("b", 100)]
    for b in range(100):
        assert pick_variant_by_weight(vs, b)[0] == "b"


def test_decide_override_skips_rules_and_bucket():
    d = decide_override(variant_key="off", reason="客服补偿", expires_at="2026-10-01T00:00:00")
    assert d.decision_path == "override"
    assert d.variant_key == "off"
    assert d.bucket_value is None
    assert d.matched_rule is None
    assert d.matched_rule_index is None
    steps = {e["step"]: e["detail"] for e in d.explanation}
    assert set(steps) == {"override_loaded", "override_selected"}
    assert steps["override_loaded"]["variant_key"] == "off"
    assert steps["override_loaded"]["reason"] == "客服补偿"
    assert steps["override_loaded"]["expires_at"] == "2026-10-01T00:00:00"


def test_decide_override_without_expiry():
    d = decide_override(variant_key="on", reason="永久白名单", expires_at=None)
    loaded = next(e for e in d.explanation if e["step"] == "override_loaded")
    assert loaded["detail"]["expires_at"] is None
