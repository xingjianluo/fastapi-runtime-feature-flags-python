"""开关组合关系校验：依赖图成环、未知依赖、互斥组冲突。

所有校验都是纯函数，输入一份 FlagSet，输出 RejectionReport；
report 非空时本次变更必须整体拒绝，继续沿用上一份有效配置。
"""
from __future__ import annotations

from typing import Any

from app.config.errors import RejectionReport, RejectReason
from app.flags.model import FlagSet


def validate_relations(flag_set: FlagSet) -> RejectionReport:
    """校验 FlagSet 中的依赖与互斥关系。

    规则：
    - 依赖目标必须存在（UNKNOWN_KEY）；
    - 依赖图不允许成环，含自依赖（DEPENDENCY_CYCLE）；
    - 同一互斥组内不允许优先级并列、不允许成员间互相依赖（RELATION_CONFLICT）。

    迭代顺序全部按名称排序，输出与声明顺序无关。
    """
    report = RejectionReport()
    flags = flag_set.flags

    # 1. 依赖目标必须存在
    for name in sorted(flags):
        for dep in flags[name].depends_on:
            if dep not in flags:
                report.add("relations", name, RejectReason.UNKNOWN_KEY,
                           f"依赖的开关 {dep!r} 不存在")

    # 2. 依赖图成环检测（仅沿存在的目标走，三色 DFS，迭代顺序确定）
    adjacency = {
        name: tuple(sorted(d for d in f.depends_on if d in flags))
        for name, f in flags.items()
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(adjacency, WHITE)
    seen_cycles: set[frozenset[str]] = set()

    def dfs(start: str) -> None:
        # 显式栈避免深图递归溢出；stack 中保存 (节点, 邻接迭代器)
        stack: list[tuple[str, Any]] = [(start, iter(adjacency[start]))]
        color[start] = GRAY
        path: list[str] = [start]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(adjacency[nxt])))
                    path.append(nxt)
                    advanced = True
                    break
                if color[nxt] == GRAY:
                    # 找到环：path 中从 nxt 开始的片段
                    cycle = path[path.index(nxt):]
                    key = frozenset(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        report.add(
                            "relations", nxt, RejectReason.DEPENDENCY_CYCLE,
                            f"依赖关系成环: {' -> '.join(cycle + [nxt])}",
                        )
            if not advanced:
                color[node] = BLACK
                stack.pop()
                path.pop()

    for name in sorted(adjacency):
        if color[name] == WHITE:
            dfs(name)

    # 3. 互斥组检查
    groups: dict[str, list[str]] = {}
    for name, f in flags.items():
        if f.mutex_group:
            groups.setdefault(f.mutex_group, []).append(name)
    for group in sorted(groups):
        members = sorted(groups[group])
        member_set = set(members)
        by_priority: dict[int, str] = {}
        for name in members:
            f = flags[name]
            if f.priority in by_priority:
                report.add(
                    "relations", name, RejectReason.RELATION_CONFLICT,
                    f"互斥组 {group!r} 内 {by_priority[f.priority]!r} 与 {name!r} 优先级并列 "
                    f"{f.priority}，无法确定唯一生效者",
                )
            else:
                by_priority[f.priority] = name
            for dep in f.depends_on:
                if dep in member_set:
                    report.add(
                        "relations", name, RejectReason.RELATION_CONFLICT,
                        f"互斥组 {group!r} 成员之间不允许依赖: {name} -> {dep}",
                    )
    return report
