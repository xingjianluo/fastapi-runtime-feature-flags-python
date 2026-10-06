"""发布前预检：变更摘要（新增/删除/改动）+ 合法性校验。

预检是纯函数：不触碰线上状态。预检不通过则不允许发布；
预检通过后的生效动作是整体替换（见 ConfigStore.refresh）。

摘要与校验均按名字排序遍历，同样的输入无论声明顺序如何，
产出的摘要与拒绝记录完全一致。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from app.config.errors import RejectionReport, RejectReason
from app.config.loader import MASK, check_type
from app.config.schema import ConfigField
from app.flags.model import FlagSet
from app.flags.relations import validate_relations


@dataclass(frozen=True)
class DiffEntry:
    """一条变更摘要：added / removed / changed。"""
    kind: str
    name: str
    detail: str = ""


@dataclass
class PreflightReport:
    """预检结论：差异摘要 + 校验拒绝记录；ok 为 False 时不允许发布。"""
    flag_diffs: list[DiffEntry] = field(default_factory=list)
    config_diffs: list[DiffEntry] = field(default_factory=list)
    rejections: RejectionReport = field(default_factory=RejectionReport)

    @property
    def ok(self) -> bool:
        return not self.rejections.has_fatal

    @property
    def summary(self) -> str:
        def tally(diffs: list[DiffEntry]) -> str:
            added = sum(1 for d in diffs if d.kind == "added")
            removed = sum(1 for d in diffs if d.kind == "removed")
            changed = sum(1 for d in diffs if d.kind == "changed")
            return f"+{added} -{removed} ~{changed}"
        return (f"开关[{tally(self.flag_diffs)}] 配置项[{tally(self.config_diffs)}] "
                f"拒绝 {len(self.rejections.rejections)} 条")


def diff_flags(old: FlagSet, new: FlagSet) -> list[DiffEntry]:
    """开关集差异：新增/删除/改动（逐字段列出变化项），按名字排序。"""
    diffs: list[DiffEntry] = []
    for name in sorted(set(old.flags) | set(new.flags)):
        old_flag, new_flag = old.flags.get(name), new.flags.get(name)
        if old_flag is None:
            diffs.append(DiffEntry("added", name, f"新增开关（类型 {new_flag.type.value}）"))
        elif new_flag is None:
            diffs.append(DiffEntry("removed", name, "删除开关"))
        elif old_flag != new_flag:
            changed = [f.name for f in dataclasses.fields(new_flag)
                       if getattr(old_flag, f.name) != getattr(new_flag, f.name)]
            diffs.append(DiffEntry("changed", name, "字段变化: " + ", ".join(changed)))
    return diffs


def diff_config(old: dict[str, Any], new: dict[str, Any],
                schema: tuple[ConfigField, ...]) -> list[DiffEntry]:
    """配置项差异；敏感字段的值在摘要中脱敏。"""
    sensitive = {f.name for f in schema if f.sensitive}
    diffs: list[DiffEntry] = []
    for name in sorted(set(old) | set(new)):
        in_old, in_new = name in old, name in new
        old_v, new_v = old.get(name), new.get(name)
        if name in sensitive:
            old_v, new_v = (MASK if old_v else old_v), (MASK if new_v else new_v)
        if not in_old:
            diffs.append(DiffEntry("added", name, f"新增配置项 = {new_v!r}"))
        elif not in_new:
            diffs.append(DiffEntry("removed", name, "删除配置项"))
        elif old.get(name) != new.get(name):
            diffs.append(DiffEntry("changed", name, f"{old_v!r} -> {new_v!r}"))
    return diffs


def validate_flag_set(flag_set: FlagSet) -> RejectionReport:
    """开关声明校验：默认值/规则值类型、百分比值域、组合关系。"""
    report = RejectionReport()
    for name in sorted(flag_set.flags):
        flag = flag_set.flags[name]
        if flag.default is not None and not check_type(flag.default, flag.type):
            report.add("flags", name, RejectReason.TYPE_MISMATCH,
                       f"默认值 {flag.default!r} 与声明类型 {flag.type.value} 不符")
        for rule in flag.rules:
            if not 0.0 <= rule.percentage <= 100.0:
                report.add("flags", name, RejectReason.INVALID_VALUE,
                           f"规则 {rule.name!r} 百分比 {rule.percentage} 超出 [0, 100]")
            if rule.value is not None and not check_type(rule.value, flag.type):
                report.add("flags", name, RejectReason.TYPE_MISMATCH,
                           f"规则 {rule.name!r} 取值 {rule.value!r} 与声明类型 {flag.type.value} 不符")
    return validate_relations(flag_set, report)


def preflight(old_config: dict[str, Any], old_flags: FlagSet,
              new_config: dict[str, Any] | None, new_flags: FlagSet,
              config_report: RejectionReport | None = None,
              schema: tuple[ConfigField, ...] = ()) -> PreflightReport:
    """对一份准备生效的新配置做预检：差异摘要 + 配置合并结论 + 开关校验。"""
    report = PreflightReport()
    if config_report is not None:
        report.rejections.rejections.extend(config_report.rejections)
    if new_config is None:
        report.rejections.add("preflight", "<config>", RejectReason.INVALID_VALUE,
                              "新配置为空（合并阶段已被拒绝）")
    else:
        report.config_diffs = diff_config(old_config, new_config, schema)
    report.flag_diffs = diff_flags(old_flags, new_flags)
    flag_report = validate_flag_set(new_flags)
    report.rejections.rejections.extend(flag_report.rejections)
    return report
