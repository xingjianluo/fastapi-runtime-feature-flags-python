"""功能开关与灰度判定。

判定语义（顺序固定、可解释）：
1. 规则按声明顺序逐条求值，第一条“全部条件命中”的规则立即生效
   （first-match-wins；因此规则顺序即冲突优先级，结论确定）。
2. 没有规则命中时，按 percentage 做稳定分桶：
   bucket = sha256(f"{flag_key}:{identity}") % 100，bucket < percentage 即命中。
   同一 (开关, 身份) 永远落在同一桶，边界可复现。
3. 均未命中则使用开关默认值。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.errors import ConfigError, Issue
from app.models import ValueType

Operator = Literal["eq", "ne", "in", "nin", "prefix", "regex", "gte", "lte"]
RuleEffect = Literal["on", "off"]


@dataclass(frozen=True)
class Condition:
    attribute: str
    op: Operator
    value: Any = None


@dataclass(frozen=True)
class Rule:
    name: str
    effect: RuleEffect
    conditions: tuple[Condition, ...] = ()


@dataclass(frozen=True)
class FlagSpec:
    key: str
    type: ValueType
    default: Any = False
    percentage: int = 0
    percentage_key: str = "id"
    rules: tuple[Rule, ...] = ()

    @staticmethod
    def from_raw(key: str, raw: Any, source: str) -> "FlagSpec":
        """从配置字典解析开关声明；非法声明整体拒绝。"""
        issues: list[Issue] = []
        if not isinstance(raw, dict):
            raise ConfigError([Issue(
                source, key, "type", "开关声明必须是对象", raw,
            )])
        type_ = raw.get("type", "bool")
        if type_ not in ("bool", "int", "float", "str", "json"):
            issues.append(Issue(source, key, "type", f"未知开关类型 {type_!r}"))
        default = raw.get("default", False)
        if not _valid_default(type_, default):
            issues.append(Issue(
                source, key, "type",
                f"默认值 {default!r} 与声明类型 {type_!r} 不符", default,
            ))
        percentage = raw.get("percentage", 0)
        if not isinstance(percentage, int) or isinstance(percentage, bool) \
                or not 0 <= percentage <= 100:
            issues.append(Issue(
                source, key, "constraint",
                f"percentage 必须是 0-100 的整数，实际 {percentage!r}",
                percentage,
            ))
        rules_raw = raw.get("rules", [])
        rules: tuple[Rule, ...] = ()
        if not isinstance(rules_raw, list):
            issues.append(Issue(source, key, "type", "rules 必须是数组"))
        else:
            parsed: list[Rule] = []
            for idx, item in enumerate(rules_raw):
                path = f"{key}.rules[{idx}]"
                rule = _parse_rule(item, path, source, issues)
                if rule is not None:
                    parsed.append(rule)
            rules = tuple(parsed)
        if issues:
            raise ConfigError(issues)
        return FlagSpec(
            key=key,
            type=type_,
            default=default,
            percentage=percentage,
            percentage_key=str(raw.get("percentage_key", "id")),
            rules=rules,
        )


def _valid_default(type_: str, default: Any) -> bool:
    if type_ == "bool":
        return isinstance(default, bool)
    if type_ == "int":
        return isinstance(default, int) and not isinstance(default, bool)
    if type_ == "float":
        return isinstance(default, (int, float)) and not isinstance(default, bool)
    if type_ == "str":
        return isinstance(default, str)
    return True  # json 接受任意值


def _parse_rule(item: Any, path: str, source: str,
                issues: list[Issue]) -> Rule | None:
    if not isinstance(item, dict):
        issues.append(Issue(source, path, "type", "规则必须是对象", item))
        return None
    name = item.get("name")
    effect = item.get("effect")
    if not isinstance(name, str) or not name:
        issues.append(Issue(source, path, "type", "规则缺少 name"))
        return None
    if effect not in ("on", "off"):
        issues.append(Issue(
            source, f"{path}.effect", "constraint",
            f"effect 只能是 on/off，实际 {effect!r}", effect,
        ))
        return None
    conds_raw = item.get("conditions", [])
    if not isinstance(conds_raw, list):
        issues.append(Issue(
            source, f"{path}.conditions", "type", "conditions 必须是数组",
        ))
        return None
    conds: list[Condition] = []
    for j, c in enumerate(conds_raw):
        cpath = f"{path}.conditions[{j}]"
        if not isinstance(c, dict) or "attribute" not in c or "op" not in c:
            issues.append(Issue(
                source, cpath, "type",
                "条件必须是含 attribute/op 的对象", c,
            ))
            continue
        op = c["op"]
        if op not in ("eq", "ne", "in", "nin", "prefix",
                      "regex", "gte", "lte"):
            issues.append(Issue(
                source, f"{cpath}.op", "constraint", f"未知操作符 {op!r}", op,
            ))
            continue
        conds.append(Condition(
            attribute=str(c["attribute"]), op=op, value=c.get("value"),
        ))
    return Rule(name=name, effect=effect, conditions=tuple(conds))


@dataclass(frozen=True)
class Decision:
    key: str
    enabled: bool
    value: Any
    basis: str            # default | percentage | rule:<name>
    detail: str
    version: int
    bucket: int | None = None
    trace: tuple[str, ...] = ()


class FlagEvaluator:
    def __init__(self, specs: dict[str, FlagSpec], version: int) -> None:
        self._specs = dict(specs)
        self._version = version

    def has(self, key: str) -> bool:
        return key in self._specs

    def with_version(self, version: int) -> "FlagEvaluator":
        """回滚时以新版本号复用同一份不可变开关声明。"""
        return FlagEvaluator(self._specs, version)

    def flag_specs(self) -> dict[str, FlagSpec]:
        return dict(self._specs)

    @staticmethod
    def stable_bucket(flag_key: str, identity: str) -> int:
        digest = hashlib.sha256(f"{flag_key}:{identity}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % 100

    def evaluate(
        self,
        key: str,
        identity: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Decision:
        attributes = dict(attributes or {})
        if key not in self._specs:
            raise KeyError(f"未声明的功能开关: {key}")
        spec = self._specs[key]
        trace: list[str] = [f"flag={key} type={spec.type} version={self._version}"]
        trace.append(
            "identity=" + (identity if identity is not None
                           else f"attr:{spec.percentage_key}="
                                f"{attributes.get(spec.percentage_key)!r}")
        )

        # 1) 规则优先，声明顺序即优先级
        for rule in spec.rules:
            results = [
                self._match(cond, attributes) for cond in rule.conditions
            ]
            matched = all(results) if rule.conditions else False
            trace.append(
                f"rule {rule.name}: "
                + (", ".join(f"{c.attribute}{c.op}={r}"
                             for c, r in zip(rule.conditions, results))
                   if rule.conditions else "无条件规则")
                + (" => MATCH" if matched else "")
            )
            if matched:
                enabled = rule.effect == "on"
                return Decision(
                    key=key, enabled=enabled,
                    value=True if enabled else spec.default,
                    basis=f"rule:{rule.name}",
                    detail=(f"规则 {rule.name} 命中，强制{('开启' if enabled else '关闭')}"
                            f"（first-match-wins，忽略后续规则）"),
                    version=self._version, trace=tuple(trace),
                )

        # 2) 百分比灰度
        ident = identity if identity is not None \
            else str(attributes.get(spec.percentage_key, ""))
        bucket = self.stable_bucket(key, ident)
        hit = bucket < spec.percentage
        trace.append(
            f"percentage: bucket={bucket} threshold={spec.percentage} "
            f"-> {'HIT' if hit else 'MISS'}"
        )
        if hit:
            return Decision(
                key=key, enabled=True, value=True, basis="percentage",
                detail=f"稳定分桶 {bucket} < {spec.percentage}，灰度命中",
                version=self._version, bucket=bucket, trace=tuple(trace),
            )

        # 3) 默认值
        trace.append(f"default: enabled={bool(spec.default)}")
        return Decision(
            key=key, enabled=bool(spec.default), value=spec.default,
            basis="default",
            detail=(f"无规则命中且分桶 {bucket} >= {spec.percentage}，"
                    f"使用默认值 {spec.default!r}"),
            version=self._version, bucket=bucket, trace=tuple(trace),
        )

    @staticmethod
    def _match(cond: Condition, attributes: dict[str, Any]) -> bool:
        if cond.attribute not in attributes:
            return False
        actual = attributes[cond.attribute]
        op = cond.op
        try:
            if op == "eq":
                return actual == cond.value
            if op == "ne":
                return actual != cond.value
            if op == "in":
                return actual in (cond.value or [])
            if op == "nin":
                return actual not in (cond.value or [])
            if op == "prefix":
                return isinstance(actual, str) and actual.startswith(str(cond.value))
            if op == "regex":
                return isinstance(actual, str) and re.search(str(cond.value), actual) is not None
            if op == "gte":
                return actual >= cond.value
            if op == "lte":
                return actual <= cond.value
        except TypeError:
            return False
        return False  # pragma: no cover
