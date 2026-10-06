"""发布前预检：对比当前快照与候选配置，给出增/删/改摘要与校验结论。

预检不修改任何运行时状态；预检不通过的候选不允许进入 refresh。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.config.errors import RejectionReport
from app.flags.model import FlagSet
from app.flags.relations import validate_relations

if TYPE_CHECKING:
    from app.runtime.store import Snapshot


@dataclass(frozen=True)
class DiffSummary:
    """一次候选发布的变更摘要（键名均按字典序，输出确定）。"""
    flags_added: tuple[str, ...] = ()
    flags_removed: tuple[str, ...] = ()
    flags_changed: tuple[str, ...] = ()
    config_added: tuple[str, ...] = ()
    config_removed: tuple[str, ...] = ()
    config_changed: tuple[str, ...] = ()


@dataclass
class PreflightResult:
    ok: bool
    summary: DiffSummary = field(default_factory=DiffSummary)
    report: RejectionReport = field(default_factory=RejectionReport)


def _diff_keys(old: dict, new: dict) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    old_keys, new_keys = set(old), set(new)
    added = tuple(sorted(new_keys - old_keys))
    removed = tuple(sorted(old_keys - new_keys))
    changed = tuple(sorted(k for k in old_keys & new_keys if old[k] != new[k]))
    return added, removed, changed


def diff_snapshot(
    current: Snapshot | None,
    config: dict[str, Any] | None,
    flags: FlagSet,
) -> DiffSummary:
    """计算候选配置相对当前快照的增/删/改。

    current 为 None 表示首次发布，所有项都算新增；
    config 为 None 表示业务配置不变（与 refresh 语义一致）。
    """
    old_flags = current.flags.flags if current is not None else {}
    fa, fr, fc = _diff_keys(old_flags, flags.flags)
    if current is None:
        ca, cr, cc = tuple(sorted((config or {}).keys())), (), ()
    elif config is None:
        ca, cr, cc = (), (), ()
    else:
        ca, cr, cc = _diff_keys(current.config, config)
    return DiffSummary(
        flags_added=fa, flags_removed=fr, flags_changed=fc,
        config_added=ca, config_removed=cr, config_changed=cc,
    )


def preflight(
    current: Snapshot | None,
    config: dict[str, Any] | None,
    flags: FlagSet,
    prior_report: RejectionReport | None = None,
) -> PreflightResult:
    """对候选发布做完整预检：变更摘要 + 组合关系校验，不改任何状态。"""
    report = RejectionReport()
    if prior_report is not None:
        report.rejections.extend(prior_report.rejections)
    report.rejections.extend(validate_relations(flags).rejections)
    return PreflightResult(ok=report.ok,
                           summary=diff_snapshot(current, config, flags),
                           report=report)
