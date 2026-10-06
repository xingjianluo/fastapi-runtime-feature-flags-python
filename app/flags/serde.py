"""开关声明的 JSON 反序列化：API/文件入口统一走这里。

未声明组合关系（depends_on/mutex_group/priority）的开关按旧语义处理；
未知字段、类型错误记入 RejectionReport。
"""
from __future__ import annotations

from typing import Any

from app.config.errors import RejectionReport, RejectReason
from app.config.schema import ValueType
from app.flags.model import Condition, Flag, FlagSet, Operator, Rule

_FLAG_FIELDS = {"name", "type", "enabled", "default", "rules", "sensitive",
                "depends_on", "mutex_group", "priority"}


def _parse_condition(data: dict[str, Any], report: RejectionReport, where: str) -> Condition | None:
    try:
        return Condition(
            attribute=str(data["attribute"]),
            operator=Operator(data["operator"]),
            value=data.get("value"),
        )
    except (KeyError, ValueError) as exc:
        report.add("flags", where, RejectReason.INVALID_VALUE, f"条件不合法: {exc}")
        return None


def _parse_rule(data: dict[str, Any], report: RejectionReport, where: str) -> Rule | None:
    name = str(data.get("name", ""))
    conditions = []
    for i, c in enumerate(data.get("conditions", [])):
        cond = _parse_condition(c, report, f"{where}.rules[{name or i}]")
        if cond is not None:
            conditions.append(cond)
    percentage = data.get("percentage", 100.0)
    if not isinstance(percentage, (int, float)) or isinstance(percentage, bool) \
            or not 0.0 <= percentage <= 100.0:
        report.add("flags", where, RejectReason.INVALID_VALUE,
                   f"规则 {name!r} 百分比越界: {percentage!r}")
        return None
    return Rule(name=name, conditions=tuple(conditions),
                percentage=float(percentage), value=data.get("value"))


def parse_flag(name: str, data: dict[str, Any], report: RejectionReport) -> Flag | None:
    """解析单个开关声明；任何字段不合法即记入 report 并返回 None。"""
    unknown = set(data) - _FLAG_FIELDS
    if unknown:
        report.add("flags", name, RejectReason.UNKNOWN_KEY,
                   f"未知字段: {sorted(unknown)}")
        return None
    try:
        flag_type = ValueType(data.get("type", "bool"))
    except ValueError:
        report.add("flags", name, RejectReason.TYPE_MISMATCH,
                   f"未知开关类型: {data.get('type')!r}")
        return None
    depends_on = data.get("depends_on", [])
    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
        report.add("flags", name, RejectReason.TYPE_MISMATCH,
                   "depends_on 必须是开关名字符串数组")
        return None
    mutex_group = data.get("mutex_group")
    if mutex_group is not None and not isinstance(mutex_group, str):
        report.add("flags", name, RejectReason.TYPE_MISMATCH, "mutex_group 必须是字符串")
        return None
    priority = data.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        report.add("flags", name, RejectReason.TYPE_MISMATCH, "priority 必须是整数")
        return None
    rules = []
    for i, r in enumerate(data.get("rules", [])):
        rule = _parse_rule(r, report, name if not r.get("name") else f"{name}.{r['name']}")
        if rule is not None:
            rules.append(rule)
    return Flag(
        name=name,
        type=flag_type,
        enabled=bool(data.get("enabled", True)),
        default=data.get("default"),
        rules=tuple(rules),
        sensitive=bool(data.get("sensitive", False)),
        depends_on=tuple(depends_on),
        mutex_group=mutex_group,
        priority=priority,
    )


def parse_flag_set(data: dict[str, Any], version: int,
                   report: RejectionReport) -> FlagSet:
    """解析 {name: flag_spec} 为 FlagSet；部分失败时其余开关仍解析，由 report 汇总。"""
    flags: dict[str, Flag] = {}
    for name in sorted(data):
        spec = data[name]
        if not isinstance(spec, dict):
            report.add("flags", name, RejectReason.TYPE_MISMATCH, "开关声明必须是对象")
            continue
        flag = parse_flag(name, spec, report)
        if flag is not None:
            flags[name] = flag
    return FlagSet(version=version, flags=flags)
