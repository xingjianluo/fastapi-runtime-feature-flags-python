"""功能开关声明：布尔/数值/字符串类型，百分比与多属性条件规则。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.config.schema import ValueType


class Operator(str, Enum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    GT = "gt"
    LT = "lt"


@dataclass(frozen=True)
class Condition:
    """单条属性条件，如 country eq 'CN'。"""
    attribute: str
    operator: Operator
    value: Any


@dataclass(frozen=True)
class Rule:
    """一条灰度规则：全部条件满足且百分比命中时返回 value。"""
    name: str
    conditions: tuple[Condition, ...] = ()
    percentage: float = 100.0  # 0~100
    value: Any = None


@dataclass(frozen=True)
class Flag:
    name: str
    type: ValueType
    enabled: bool = True
    default: Any = None
    rules: tuple[Rule, ...] = ()
    sensitive: bool = False


@dataclass(frozen=True)
class FlagSet:
    """一份完整、不可变的开关配置快照。"""
    version: int
    flags: dict[str, Flag] = field(default_factory=dict)
