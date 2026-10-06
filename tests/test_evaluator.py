import logging

from app.config.schema import ValueType
from app.flags.evaluator import DecisionKind, evaluate
from app.flags.model import Condition, Flag, FlagSet, Operator, Rule

log = logging.getLogger(__name__)


def make_flag_set(*flags: Flag, version: int = 1) -> FlagSet:
    return FlagSet(version=version, flags={f.name: f for f in flags})


def show(decision, context):
    log.info("输入 flag=%s context=%s -> 判定=%s 值=%r 规则=%s 原因=%s",
             decision.flag, context, decision.kind.value, decision.value,
             decision.rule, decision.reason)
    return decision


def test_missing_flag():
    fs = make_flag_set()
    d = show(evaluate(fs, "nope", {}), {})
    assert d.kind is DecisionKind.FLAG_MISSING
    assert d.value is None


def test_disabled_flag_returns_default():
    fs = make_flag_set(Flag("dark_mode", ValueType.BOOL, enabled=False, default=False))
    d = show(evaluate(fs, "dark_mode", {"user_id": "u1"}), {"user_id": "u1"})
    assert d.kind is DecisionKind.FLAG_DISABLED
    assert d.value is False


def test_no_rule_matched_returns_default():
    flag = Flag("rate", ValueType.INT, default=1, rules=(
        Rule("vip", conditions=(Condition("tier", Operator.EQ, "vip"),), value=10),
    ))
    fs = make_flag_set(flag)
    ctx = {"user_id": "u1", "tier": "free"}
    d = show(evaluate(fs, "rate", ctx), ctx)
    assert d.kind is DecisionKind.DEFAULT
    assert d.value == 1


def test_rule_match():
    flag = Flag("rate", ValueType.INT, default=1, rules=(
        Rule("vip", conditions=(Condition("tier", Operator.EQ, "vip"),), value=10),
    ))
    fs = make_flag_set(flag)
    ctx = {"user_id": "u1", "tier": "vip"}
    d = show(evaluate(fs, "rate", ctx), ctx)
    assert d.kind is DecisionKind.RULE_MATCH
    assert d.value == 10
    assert d.rule == "vip"


def test_percentage_deterministic_across_users():
    flag = Flag("rollout", ValueType.BOOL, default=False, rules=(
        Rule("half", percentage=50.0, value=True),
    ))
    fs = make_flag_set(flag)
    results = {}
    for i in range(200):
        uid = f"user-{i}"
        d = evaluate(fs, "rollout", {"user_id": uid})
        results[uid] = d.value
        # 桶内 RULE_MATCH=True，桶外 DEFAULT=False（默认值）
        if d.kind is DecisionKind.RULE_MATCH:
            assert d.value is True
        else:
            assert d.kind is DecisionKind.DEFAULT and d.value is False
    on_ratio = sum(results.values()) / len(results)
    log.info("50%% 灰度 200 个用户命中比例=%.2f", on_ratio)
    assert 0.3 < on_ratio < 0.7
    # 同一用户重复求值结果稳定
    for uid, v in results.items():
        assert evaluate(fs, "rollout", {"user_id": uid}).value == v


def test_rule_conflict_falls_back_to_default():
    flag = Flag("rate", ValueType.INT, default=1, rules=(
        Rule("a", conditions=(Condition("tier", Operator.EQ, "vip"),), value=10),
        Rule("b", conditions=(Condition("country", Operator.EQ, "CN"),), value=20),
    ))
    fs = make_flag_set(flag)
    ctx = {"tier": "vip", "country": "CN"}
    d = show(evaluate(fs, "rate", ctx), ctx)
    assert d.kind is DecisionKind.RULE_CONFLICT
    assert d.value == 1
    assert "a" in d.reason and "b" in d.reason


def test_multiple_rules_same_value_no_conflict():
    flag = Flag("rate", ValueType.INT, default=1, rules=(
        Rule("a", conditions=(Condition("tier", Operator.EQ, "vip"),), value=10),
        Rule("b", conditions=(Condition("country", Operator.EQ, "CN"),), value=10),
    ))
    fs = make_flag_set(flag)
    d = show(evaluate(fs, "rate", {"tier": "vip", "country": "CN"}), {})
    assert d.kind is DecisionKind.RULE_MATCH
    assert d.value == 10
