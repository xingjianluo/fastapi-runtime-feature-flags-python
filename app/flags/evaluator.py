"""开关求值：命中/未命中/冲突均给出确定且可解释的判定。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.flags.model import FlagSet


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


def evaluate(flag_set: FlagSet, name: str, context: dict[str, Any]) -> Decision:
    """对单个开关求值（骨架：空实现）。"""
    raise NotImplementedError
