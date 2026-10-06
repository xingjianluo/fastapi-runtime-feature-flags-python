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
from typing import TYPE_CHECKING, Any

from app.flags.model import Condition, Flag, FlagSet, Operator, Rule

if TYPE_CHECKING:
    from app.runtime.overrides import OverrideManager


class DecisionKind(str, Enum):
    RULE_MATCH = "rule_match"        # 命中某条规则
    DEFAULT = "default"              # 未命中任何规则，取默认值
    FLAG_DISABLED = "flag_disabled"  # 开关关闭
    FLAG_MISSING = "flag_missing"    # 开关不存在
    RULE_CONFLICT = "rule_conflict"  # 多条规则命中且取值不一致
    DEPENDENCY_UNSATISFIED = "dependency_unsatisfied"  # 依赖的开关未生效
    GROUP_EXCLUDED = "group_excluded"  # 互斥组内其他开关生效，本开关被排除
    OVERRIDE = "override"            # 临时覆盖生效中，强制取值


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


_ACTIVE_KINDS = frozenset({DecisionKind.RULE_MATCH, DecisionKind.DEFAULT, DecisionKind.OVERRIDE})


def _evaluate_rules(flag_set: FlagSet, flag: Flag, context: dict[str, Any]) -> Decision:
    """规则求值：按声明顺序匹配，多规则取值冲突时确定性地回退默认值。"""
    bucketing_key = _bucket_key(context)
    matched = [r for r in flag.rules if _match_rule(flag, r, context, bucketing_key)]
    if not matched:
        return Decision(flag=flag.name, kind=DecisionKind.DEFAULT, value=flag.default,
                        reason="未命中任何规则，返回默认值", version=flag_set.version)
    values = {r.value for r in matched}
    if len(values) > 1:
        names = ", ".join(r.name for r in matched)
        return Decision(flag=flag.name, kind=DecisionKind.RULE_CONFLICT, value=flag.default,
                        reason=f"多条规则命中且取值不一致（{names}），回退默认值",
                        version=flag_set.version)
    rule = matched[0]
    value = rule.value if rule.value is not None else flag.default
    return Decision(flag=flag.name, kind=DecisionKind.RULE_MATCH, value=value, rule=rule.name,
                    reason=f"命中规则 {rule.name!r}", version=flag_set.version)


def _is_active(decision: Decision) -> bool:
    """判定是否为“生效”：命中规则或取默认值且值为真。关闭/缺失/被排除均不算。"""
    return decision.kind in _ACTIVE_KINDS and bool(decision.value)


def _evaluate_own(
    flag_set: FlagSet,
    name: str,
    context: dict[str, Any],
    own_memo: dict[str, Decision],
    final_memo: dict[str, Decision],
    in_progress: set[str],
    overrides: OverrideManager | None,
) -> Decision:
    """开关自身的判定（缺失/关闭/依赖/规则），不做互斥组裁决。"""
    if name in own_memo:
        return own_memo[name]
    flag = flag_set.flags.get(name)
    if flag is None:
        d = Decision(flag=name, kind=DecisionKind.FLAG_MISSING, value=None,
                     reason=f"开关 {name!r} 不存在", version=flag_set.version)
    elif not flag.enabled:
        d = Decision(flag=name, kind=DecisionKind.FLAG_DISABLED, value=flag.default,
                     reason="开关已关闭，返回默认值", version=flag_set.version)
    else:
        unsatisfied = None
        for dep in sorted(set(flag.depends_on)):
            dep_decision = _evaluate_final(flag_set, dep, context, own_memo, final_memo,
                                           in_progress, overrides)
            if not _is_active(dep_decision):
                unsatisfied = dep
                break
        if unsatisfied is not None:
            d = Decision(flag=name, kind=DecisionKind.DEPENDENCY_UNSATISFIED, value=flag.default,
                         reason=f"依赖的开关 {unsatisfied!r} 未生效，本开关强制不生效",
                         version=flag_set.version)
        else:
            d = _evaluate_rules(flag_set, flag, context)
    own_memo[name] = d
    return d


def _evaluate_final(
    flag_set: FlagSet,
    name: str,
    context: dict[str, Any],
    own_memo: dict[str, Decision],
    final_memo: dict[str, Decision],
    in_progress: set[str],
    overrides: OverrideManager | None,
) -> Decision:
    """完整判定 = 临时覆盖（最高优先级）> 自身判定 + 互斥组裁决。"""
    if name in final_memo:
        return final_memo[name]
    if name in in_progress:
        # 防御：发布前校验会拒绝依赖环，正常不会走到这里
        return Decision(flag=name, kind=DecisionKind.DEPENDENCY_UNSATISFIED, value=None,
                        reason="依赖关系成环", version=flag_set.version)
    in_progress.add(name)
    flag = flag_set.flags.get(name)
    if flag is not None and overrides is not None:
        override = overrides.get(name)
        if override is not None:
            decision = Decision(
                flag=name, kind=DecisionKind.OVERRIDE, value=override.value,
                reason=f"临时覆盖生效中（{override.actor} 设置，到期时间 {override.expires_at}）",
                version=flag_set.version,
            )
            in_progress.discard(name)
            final_memo[name] = decision
            return decision
    own = _evaluate_own(flag_set, name, context, own_memo, final_memo, in_progress, overrides)
    decision = own
    if flag is not None and flag.exclusive_group and _is_active(own):
        members = flag_set.group_index.get(flag.exclusive_group, ())
        active = [m for m in members
                  if _is_active(_evaluate_own(flag_set, m, context, own_memo, final_memo,
                                              in_progress, overrides))]
        winner = min(active) if active else name
        if winner != name:
            decision = Decision(
                flag=name, kind=DecisionKind.GROUP_EXCLUDED, value=flag.default,
                reason=f"互斥组 {flag.exclusive_group!r} 中 {winner!r} 生效，本开关被排除",
                version=flag_set.version,
            )
    in_progress.discard(name)
    final_memo[name] = decision
    return decision


def evaluate(flag_set: FlagSet, name: str, context: dict[str, Any],
             overrides: OverrideManager | None = None) -> Decision:
    """对单个开关求值：覆盖 > 关闭/依赖/规则 > 互斥裁决，结论与声明顺序、并发交错无关。"""
    return _evaluate_final(flag_set, name, context, {}, {}, set(), overrides)
