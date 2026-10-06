"""开关求值：命中/未命中/冲突均给出确定且可解释的判定。

确定性保证：
- 百分比分桶使用 sha256（跨进程、跨声明顺序稳定），不依赖 Python 内置 hash；
- 多规则命中且取值不一致时，确定性地回退默认值并标记 RULE_CONFLICT；
- 求值只读取不可变 FlagSet 快照，不加锁，并发读结果一致。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.flags.model import Condition, Flag, FlagSet, Operator, Rule


class DecisionKind(str, Enum):
    RULE_MATCH = "rule_match"        # 命中某条规则
    DEFAULT = "default"              # 未命中任何规则，取默认值
    FLAG_DISABLED = "flag_disabled"  # 开关关闭
    FLAG_MISSING = "flag_missing"    # 开关不存在
    RULE_CONFLICT = "rule_conflict"  # 多条规则命中且取值不一致


@dataclass(frozen=True)
class Decision:
    flag: str
    kind: DecisionKind
    value: Any
    rule: str | None = None
    reason: str = ""
    version: int = 0


def _bucket(flag_name: str, rule_name: str, key: str) -> float:
    """把 (开关, 规则, 用户键) 稳定映射到 [0, 100) 的桶位。"""
    digest = hashlib.sha256(f"{flag_name}:{rule_name}:{key}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 10000 / 100.0


def _bucket_key(context: dict[str, Any]) -> str:
    """分桶键：优先 user_id，其次 key，都没有则为空串（所有请求同一桶）。"""
    for attr in ("user_id", "key"):
        if attr in context and context[attr] is not None:
            return str(context[attr])
    return ""


def _match_condition(cond: Condition, context: dict[str, Any]) -> bool:
    """单条件匹配；属性缺失时一律不命中（确定性语义）。"""
    if cond.attribute not in context:
        return False
    actual = context[cond.attribute]
    expected = cond.value
    if cond.operator is Operator.EQ:
        return actual == expected
    if cond.operator is Operator.NE:
        return actual != expected
    if cond.operator is Operator.IN:
        return isinstance(expected, (list, tuple, set, frozenset)) and actual in expected
    if cond.operator is Operator.GT:
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual > expected
    if cond.operator is Operator.LT:
        return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual < expected
    return False


def _match_rule(flag: Flag, rule: Rule, context: dict[str, Any], bucketing_key: str) -> bool:
    """规则命中 = 全部条件满足 且 百分比分桶命中。"""
    if not all(_match_condition(c, context) for c in rule.conditions):
        return False
    if rule.percentage <= 0:
        return False
    if rule.percentage >= 100:
        return True
    return _bucket(flag.name, rule.name, bucketing_key) < rule.percentage


def evaluate(flag_set: FlagSet, name: str, context: dict[str, Any]) -> Decision:
    """对单个开关求值：规则按声明顺序匹配，结论与声明顺序、并发交错无关。"""
    flag = flag_set.flags.get(name)
    if flag is None:
        return Decision(flag=name, kind=DecisionKind.FLAG_MISSING, value=None,
                        reason=f"开关 {name!r} 不存在", version=flag_set.version)
    if not flag.enabled:
        return Decision(flag=name, kind=DecisionKind.FLAG_DISABLED, value=flag.default,
                        reason="开关已关闭，返回默认值", version=flag_set.version)

    bucketing_key = _bucket_key(context)
    matched = [r for r in flag.rules if _match_rule(flag, r, context, bucketing_key)]
    if not matched:
        return Decision(flag=name, kind=DecisionKind.DEFAULT, value=flag.default,
                        reason="未命中任何规则，返回默认值", version=flag_set.version)

    values = {r.value for r in matched}
    if len(values) > 1:
        names = ", ".join(r.name for r in matched)
        return Decision(flag=name, kind=DecisionKind.RULE_CONFLICT, value=flag.default,
                        reason=f"多条规则命中且取值不一致（{names}），回退默认值",
                        version=flag_set.version)
    rule = matched[0]
    value = rule.value if rule.value is not None else flag.default
    return Decision(flag=name, kind=DecisionKind.RULE_MATCH, value=value, rule=rule.name,
                    reason=f"命中规则 {rule.name!r}", version=flag_set.version)
