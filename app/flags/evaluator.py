"""开关求值：命中/未命中/冲突均给出确定且可解释的判定。

求值是纯函数：输入不可变 FlagSet + 上下文（+ 可选覆盖管理器），
不依赖声明顺序与并发交错，同一请求内结论一致。

优先级（高 -> 低）：临时覆盖 > 依赖未满足强制关闭 > 互斥组裁决 > 规则/默认值。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.flags.model import Condition, Flag, FlagSet, Operator, Rule
from app.flags.overrides import OverrideManager


class DecisionKind(str, Enum):
    RULE_MATCH = "rule_match"        # 命中某条规则
    DEFAULT = "default"              # 未命中任何规则，取默认值
    FLAG_DISABLED = "flag_disabled"  # 开关关闭
    FLAG_MISSING = "flag_missing"    # 开关不存在
    RULE_CONFLICT = "rule_conflict"  # 多条规则命中且取值不一致
    OVERRIDE = "override"                    # 临时覆盖生效
    DEPENDENCY_UNSATISFIED = "dependency_unsatisfied"  # 依赖未满足，强制关闭
    MUTEX_LOST = "mutex_lost"                # 互斥组内落选，强制关闭


@dataclass(frozen=True)
class Decision:
    flag: str
    kind: DecisionKind
    value: Any
    rule: str | None = None
    reason: str = ""
    version: int = 0


def _off_value(flag: Flag) -> Any:
    """开关被强制关闭时的取值。"""
    return False if flag.type.value == "bool" else None


def _bucket_key(context: dict[str, Any]) -> str:
    if "id" in context:
        return str(context["id"])
    return repr(sorted(context.items(), key=lambda kv: kv[0]))


def _in_percentage(flag_name: str, context: dict[str, Any], percentage: float) -> bool:
    """稳定哈希分桶：同一 flag + 同一上下文永远落在同一桶。"""
    if percentage >= 100.0:
        return True
    if percentage <= 0.0:
        return False
    digest = hashlib.sha256(f"{flag_name}|{_bucket_key(context)}".encode()).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return bucket < percentage


def _match_condition(cond: Condition, context: dict[str, Any]) -> bool:
    if cond.attribute not in context:
        return False
    actual = context[cond.attribute]
    try:
        if cond.operator is Operator.EQ:
            return actual == cond.value
        if cond.operator is Operator.NE:
            return actual != cond.value
        if cond.operator is Operator.IN:
            return actual in cond.value
        if cond.operator is Operator.GT:
            return actual > cond.value
        if cond.operator is Operator.LT:
            return actual < cond.value
    except TypeError:
        return False
    return False


def _is_active(decision: Decision) -> bool:
    """判定结果是否为"生效"：作为依赖满足与互斥裁决的统一标准。"""
    return decision.kind in (
        DecisionKind.RULE_MATCH,
        DecisionKind.DEFAULT,
        DecisionKind.OVERRIDE,
    ) and bool(decision.value)


class _Evaluator:
    """单次求值的内部状态：请求级 memo，保证链式依赖只算一次。"""

    def __init__(self, flag_set: FlagSet, context: dict[str, Any],
                 overrides: OverrideManager | None) -> None:
        self._flags = flag_set.flags
        self._version = flag_set.version
        self._context = context
        self._overrides = overrides
        self._memo: dict[str, Decision] = {}
        self._local_memo: dict[str, Decision] = {}

    def _local_cached(self, name: str, flag: Flag) -> Decision:
        if name not in self._local_memo:
            self._local_memo[name] = self._local(name, flag)
        return self._local_memo[name]

    def evaluate(self, name: str) -> Decision:
        if name in self._memo:
            return self._memo[name]
        # 占位防环：校验期已拒绝成环声明，这里仅作防御
        self._memo[name] = Decision(name, DecisionKind.FLAG_MISSING, None,
                                    reason="evaluation loop", version=self._version)
        decision = self._compute(name)
        self._memo[name] = decision
        return decision

    def _compute(self, name: str) -> Decision:
        flag = self._flags.get(name)
        if flag is None:
            return Decision(name, DecisionKind.FLAG_MISSING, None,
                            reason="开关不存在", version=self._version)

        # 1. 临时覆盖优先于一切规则判定
        if self._overrides is not None:
            override = self._overrides.active_for(name)
            if override is not None:
                return Decision(name, DecisionKind.OVERRIDE, override.value,
                                reason=f"临时覆盖生效（{override.actor}: {override.reason}）",
                                version=self._version)

        # 2. 依赖未满足则强制关闭（依赖按名称排序遍历，顺序无关）
        for dep in sorted(flag.depends_on):
            dep_decision = self.evaluate(dep)
            if not _is_active(dep_decision):
                return Decision(name, DecisionKind.DEPENDENCY_UNSATISFIED,
                                _off_value(flag),
                                reason=f"依赖的开关 {dep!r} 未生效（{dep_decision.kind.value}）",
                                version=self._version)

        # 3. 互斥组裁决：本开关局部生效但非组内最高优先级时强制关闭
        if flag.mutex_group:
            local = self._local_cached(name, flag)
            if _is_active(local):
                winner = self._group_winner(flag.mutex_group)
                if winner is not None and winner != name:
                    return Decision(name, DecisionKind.MUTEX_LOST, _off_value(flag),
                                    reason=f"互斥组 {flag.mutex_group!r} 内 {winner!r} 优先生效",
                                    version=self._version)
            return local

        return self._local_cached(name, flag)

    def _local(self, name: str, flag: Flag) -> Decision:
        """不考虑组合关系的本地判定：enabled + 规则 + 默认值。"""
        if not flag.enabled:
            return Decision(name, DecisionKind.FLAG_DISABLED, _off_value(flag),
                            reason="开关已关闭", version=self._version)
        matched: list[Rule] = [
            r for r in flag.rules
            if all(_match_condition(c, self._context) for c in r.conditions)
            and _in_percentage(name, self._context, r.percentage)
        ]
        if not matched:
            return Decision(name, DecisionKind.DEFAULT, flag.default,
                            reason="未命中任何规则", version=self._version)
        values = {repr(r.value) for r in matched}
        if len(values) > 1:
            return Decision(name, DecisionKind.RULE_CONFLICT, flag.default,
                            reason="多条规则命中且取值不一致: "
                                   + ",".join(r.name for r in matched),
                            version=self._version)
        return Decision(name, DecisionKind.RULE_MATCH, matched[0].value,
                        rule=matched[0].name, reason="命中规则",
                        version=self._version)

    def _group_winner(self, group: str) -> str | None:
        """互斥组内局部生效成员中优先级最高者（并列时按名称，防御性兜底）。"""
        candidates: list[tuple[int, str]] = []
        for name in sorted(self._flags):
            f = self._flags[name]
            if f.mutex_group != group:
                continue
            if _is_active(self._local_cached(name, f)):
                candidates.append((f.priority, name))
        if not candidates:
            return None
        # 优先级大者胜；并列按名称字典序，保证确定
        return max(candidates, key=lambda t: (t[0], t[1]))[1]


def evaluate(flag_set: FlagSet, name: str, context: dict[str, Any],
             overrides: OverrideManager | None = None) -> Decision:
    """对单个开关求值：覆盖 > 依赖 > 互斥 > 规则/默认。"""
    return _Evaluator(flag_set, context, overrides).evaluate(name)
