"""功能开关声明：布尔/数值/字符串类型，百分比与多属性条件规则。

组合关系（向后兼容，缺省为空即旧语义）：
- depends_on：依赖的开关名列表；依赖未满足时本开关强制不生效；
- exclusive_group：互斥组名；同组同一时刻至多一个开关生效。
"""
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
    depends_on: tuple[str, ...] = ()
    exclusive_group: str | None = None


@dataclass(frozen=True)
class FlagSet:
    """一份完整、不可变的开关配置快照。

    group_index 在构造时预计算（组名 -> 排序后的成员开关名），
    求值时直接查表，不随开关数量增加出现额外开销。
    """
    version: int
    flags: dict[str, Flag] = field(default_factory=dict)
    group_index: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        groups: dict[str, list[str]] = {}
        for name, flag in self.flags.items():
            if flag.exclusive_group:
                groups.setdefault(flag.exclusive_group, []).append(name)
        # 成员按名字排序：互斥裁决与声明顺序无关
        object.__setattr__(self, "group_index",
                           {g: tuple(sorted(members)) for g, members in groups.items()})
