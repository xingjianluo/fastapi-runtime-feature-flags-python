"""把 JSON 字典解析为 FlagSet；非法条目记录拒绝并跳过，绝不静默猜值。

解析只做“形状”校验（字段类型、枚举合法）；值域与组合关系校验由
preflight.validate_flag_set 在发布前统一完成。
"""
from __future__ import annotations

from typing import Any

from app.config.errors import RejectionReport, RejectReason
from app.config.schema import ValueType
from app.flags.model import Condition, Flag, FlagSet, Operator, Rule

SOURCE = "flags_json"

_FLAG_KEYS = {"type", "enabled", "default", "rules", "sensitive",
              "depends_on", "exclusive_group"}
_RULE_KEYS = {"name", "conditions", "percentage", "value"}
_COND_KEYS = {"attribute", "operator", "value"}


def _reject_unknown_keys(report: RejectionReport, key: str, data: dict, allowed: set[str]) -> bool:
    unknown = sorted(set(data) - allowed)
    for k in unknown:
        report.add(SOURCE, key, RejectReason.UNKNOWN_KEY, f"未知字段 {k!r}")
    return not unknown


def _parse_condition(key: str, data: Any, report: RejectionReport) -> Condition | None:
    if not isinstance(data, dict) or not _reject_unknown_keys(report, key, data, _COND_KEYS):
        report.add(SOURCE, key, RejectReason.TYPE_MISMATCH, "条件必须是对象且字段合法")
        return None
    attribute, operator, value = data.get("attribute"), data.get("operator"), data.get("value")
    if not isinstance(attribute, str) or not attribute:
        report.add(SOURCE, key, RejectReason.TYPE_MISMATCH, "条件 attribute 必须是非空字符串")
        return None
    try:
        op = Operator(operator)
    except ValueError:
        report.add(SOURCE, key, RejectReason.INVALID_VALUE, f"未知比较符 {operator!r}")
        return None
    return Condition(attribute=attribute, operator=op, value=value)


def _parse_rule(flag_name: str, data: Any, report: RejectionReport) -> Rule | None:
    key = f"{flag_name}.rules"
    if not isinstance(data, dict) or not _reject_unknown_keys(report, key, data, _RULE_KEYS):
        report.add(SOURCE, key, RejectReason.TYPE_MISMATCH, "规则必须是对象且字段合法")
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name:
        report.add(SOURCE, key, RejectReason.TYPE_MISMATCH, "规则 name 必须是非空字符串")
        return None
    percentage = data.get("percentage", 100.0)
    if not isinstance(percentage, (int, float)) or isinstance(percentage, bool):
        report.add(SOURCE, key, RejectReason.TYPE_MISMATCH, f"规则 {name!r} 百分比必须是数值")
        return None
    conditions = []
    for i, c in enumerate(data.get("conditions", [])):
        cond = _parse_condition(f"{flag_name}.rules[{name}].conditions[{i}]", c, report)
        if cond is None:
            return None
        conditions.append(cond)
    return Rule(name=name, conditions=tuple(conditions),
                percentage=float(percentage), value=data.get("value"))


def _parse_flag(name: str, data: Any, report: RejectionReport) -> Flag | None:
    if not isinstance(data, dict) or not _reject_unknown_keys(report, name, data, _FLAG_KEYS):
        report.add(SOURCE, name, RejectReason.TYPE_MISMATCH, "开关声明必须是对象且字段合法")
        return None
    try:
        value_type = ValueType(data.get("type"))
    except ValueError:
        report.add(SOURCE, name, RejectReason.INVALID_VALUE,
                   f"未知类型 {data.get('type')!r}，可选: {[t.value for t in ValueType]}")
        return None
    depends_on = data.get("depends_on", [])
    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
        report.add(SOURCE, name, RejectReason.TYPE_MISMATCH, "depends_on 必须是字符串数组")
        return None
    group = data.get("exclusive_group")
    if group is not None and not isinstance(group, str):
        report.add(SOURCE, name, RejectReason.TYPE_MISMATCH, "exclusive_group 必须是字符串")
        return None
    rules = []
    for r in data.get("rules", []):
        rule = _parse_rule(name, r, report)
        if rule is None:
            return None
        rules.append(rule)
    enabled = data.get("enabled", True)
    sensitive = data.get("sensitive", False)
    if not isinstance(enabled, bool) or not isinstance(sensitive, bool):
        report.add(SOURCE, name, RejectReason.TYPE_MISMATCH, "enabled/sensitive 必须是布尔值")
        return None
    return Flag(name=name, type=value_type, enabled=enabled, default=data.get("default"),
                rules=tuple(rules), sensitive=sensitive,
                depends_on=tuple(depends_on), exclusive_group=group)


def parse_flag_set(data: Any, version: int = 0,
                   report: RejectionReport | None = None) -> tuple[FlagSet, RejectionReport]:
    """解析 {"flags": {name: {...}}} 为 FlagSet；非法开关跳过并记录拒绝。"""
    if report is None:
        report = RejectionReport()
    if not isinstance(data, dict) or not isinstance(data.get("flags", {}), dict):
        report.add(SOURCE, "<root>", RejectReason.TYPE_MISMATCH,
                   "请求体必须是 {\"flags\": {name: {...}}} 形式的对象")
        return FlagSet(version=version), report
    flags: dict[str, Flag] = {}
    for name, raw in data["flags"].items():
        if not isinstance(name, str) or not name:
            report.add(SOURCE, "<root>", RejectReason.INVALID_VALUE, "开关名必须是非空字符串")
            continue
        flag = _parse_flag(name, raw, report)
        if flag is not None:
            flags[name] = flag
    return FlagSet(version=version, flags=flags), report
