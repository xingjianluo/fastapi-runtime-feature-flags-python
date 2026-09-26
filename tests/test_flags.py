"""灰度命中边界、规则优先级与可解释判定。"""
from __future__ import annotations

import logging

import pytest

from app.errors import ConfigError
from app.flags import FlagEvaluator, FlagSpec

log = logging.getLogger("tests")


def _flag(**kw):
    raw = {"type": "bool", "default": False, "percentage": 0}
    raw.update(kw)
    return FlagSpec.from_raw("f", raw, "file:config.json")


def _find_identity(evaluator, target_bucket):
    i = 0
    while evaluator.stable_bucket("f", f"u{i}") != target_bucket:
        i += 1
        if i > 100000:
            raise AssertionError("未找到目标桶")
    return f"u{i}"


def test_default_off():
    ev = FlagEvaluator({"f": _flag()}, 1)
    log.info("输入: percentage=0, 无规则身份")
    d = ev.evaluate("f", identity="anyone")
    assert d.enabled is False and d.basis == "default"
    log.info("决策: enabled=%s basis=%s detail=%s", d.enabled, d.basis, d.detail)


def test_percentage_boundary():
    ev = FlagEvaluator({"f": _flag(percentage=50)}, 1)
    b49 = _find_identity(ev, 49)
    b50 = _find_identity(ev, 50)
    log.info("输入: percentage=50, bucket=49 身份=%s / bucket=50 身份=%s", b49, b50)

    d49 = ev.evaluate("f", b49)
    d50 = ev.evaluate("f", b50)
    assert d49.bucket == 49 and d49.enabled is True and d49.basis == "percentage"
    assert d50.bucket == 50 and d50.enabled is False and d50.basis == "default"
    log.info("决策边界: bucket49 -> %s(%s); bucket50 -> %s(%s)",
             d49.enabled, d49.basis, d50.enabled, d50.basis)


def test_percentage_zero_and_hundred():
    ev0 = FlagEvaluator({"f": _flag(percentage=0)}, 1)
    ev100 = FlagEvaluator({"f": _flag(percentage=100)}, 1)
    assert ev0.evaluate("f", "x").enabled is False
    assert ev100.evaluate("f", "x").enabled is True
    # 桶 99 在 100% 下命中
    assert ev100.stable_bucket("f", "x") in range(100)


def test_stable_bucket_is_deterministic():
    for _ in range(5):
        assert FlagEvaluator.stable_bucket("f", "user-123") == \
            FlagEvaluator.stable_bucket("f", "user-123")
    # 不同开关的同一身份不要求相同桶，但都在 0-99
    b = FlagEvaluator.stable_bucket("g", "user-123")
    assert 0 <= b <= 99


def test_rule_first_match_wins_on_conflict():
    # staff 且 free：第一条 on 规则先命中，第二条 off 被忽略 -> 确定结论
    ev = FlagEvaluator({"f": _flag(
        percentage=0,
        rules=[
            {"name": "staff", "effect": "on",
             "conditions": [{"attribute": "email", "op": "prefix",
                             "value": "staff@"}]},
            {"name": "free_off", "effect": "off",
             "conditions": [{"attribute": "tier", "op": "eq", "value": "free"}]},
        ],
    )}, 1)
    attrs = {"email": "staff@x.com", "tier": "free"}
    log.info("输入属性=%s（两条规则同时满足，冲突）", attrs)
    d = ev.evaluate("f", identity="u1", attributes=attrs)
    assert d.basis == "rule:staff" and d.enabled is True
    log.info("决策: basis=%s trace=%s", d.basis, list(d.trace))


def test_rule_off_overrides_percentage():
    ev = FlagEvaluator({"f": _flag(
        percentage=100,
        rules=[
            {"name": "free_off", "effect": "off",
             "conditions": [{"attribute": "tier", "op": "eq", "value": "free"}]},
        ],
    )}, 1)
    d = ev.evaluate("f", identity="u1", attributes={"tier": "free"})
    assert d.basis == "rule:free_off" and d.enabled is False
    log.info("决策（规则 off 压过 100%% 灰度）: %s", d.detail)


def test_non_matching_attributes_explainable():
    ev = FlagEvaluator({"f": _flag(
        rules=[{"name": "r", "effect": "on",
                "conditions": [{"attribute": "region", "op": "eq",
                                "value": "cn"}]}]
    )}, 1)
    d = ev.evaluate("f", identity="u1", attributes={"region": "us"})
    assert d.enabled is False and d.basis == "default"
    assert any("regioneq=False" in t for t in d.trace)
    log.info("未命中判定链: %s", list(d.trace))


def test_operators():
    ev = FlagEvaluator({"f": _flag(rules=[
        {"name": "in_rule", "effect": "on",
         "conditions": [{"attribute": "id", "op": "in", "value": ["a", "b"]}]},
        {"name": "gte_rule", "effect": "on",
         "conditions": [{"attribute": "age", "op": "gte", "value": 18}]},
        {"name": "regex_rule", "effect": "on",
         "conditions": [{"attribute": "ua", "op": "regex", "value": "^iOS"}]},
    ])}, 1)
    assert ev.evaluate("f", attributes={"id": "b"}).enabled is True
    assert ev.evaluate("f", attributes={"id": "c", "age": 20}).enabled is True
    assert ev.evaluate("f", attributes={"id": "c", "age": 2}).enabled is False
    assert ev.evaluate("f", attributes={"id": "c", "age": 2,
                                        "ua": "iOS/17"}).enabled is True


@pytest.mark.parametrize("bad", [
    {"type": "bool", "percentage": 101},
    {"type": "bool", "percentage": -1},
    {"type": "bool", "rules": [{"name": "r", "effect": "maybe"}]},
    {"type": "int", "default": "not-int"},
    {"type": "bool", "rules": [{"effect": "on"}]},
])
def test_invalid_flag_declaration_rejected(bad):
    log.info("非法开关声明输入: %s", bad)
    with pytest.raises(ConfigError) as exc:
        FlagSpec.from_raw("bad_flag", bad, "file:config.json")
    assert exc.value.issues
    log.info("拒绝结论: %s", exc.value.explain())


def test_unknown_flag_key_raises():
    ev = FlagEvaluator({}, 1)
    with pytest.raises(KeyError):
        ev.evaluate("nope")
