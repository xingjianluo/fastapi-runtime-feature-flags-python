"""开关组合关系校验：依赖成环、未知依赖、互斥组静态冲突。

校验发生在发布前（预检），任何一条关系不合法都整体拒绝本次变更，
并指出具体开关与关系；运行期求值不再重复校验，只做确定性裁决。

确定性保证：所有遍历按开关名排序进行，环按最小成员名归一化，
同样的关系图无论声明顺序如何，产出的拒绝记录完全一致。
"""
from __future__ import annotations

from app.config.errors import RejectionReport, RejectReason
from app.flags.model import Flag, FlagSet

SOURCE = "relations"


def _is_unconditionally_active(flag: Flag) -> bool:
    """开关是否“无条件生效”：启用、无规则、默认值真值 —— 同组两个即静态冲突。"""
    return flag.enabled and not flag.rules and bool(flag.default)


def _find_cycles(flag_set: FlagSet) -> list[tuple[str, ...]]:
    """找出 depends_on 图中的全部环，按最小成员归一化并去重，结果确定。"""
    deps = {name: tuple(sorted(set(flag.depends_on) & flag_set.flags.keys()))
            for name, flag in flag_set.flags.items()}
    cycles: set[tuple[str, ...]] = set()
    state: dict[str, int] = {}  # 0=未访问 1=在栈中 2=已完成

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in deps.get(node, ()):
            if state.get(nxt, 0) == 0:
                visit(nxt, stack)
            elif state.get(nxt) == 1:
                cycle = stack[stack.index(nxt):]
                # 归一化：从字典序最小成员开始旋转，保证同一环只报告一次
                i = cycle.index(min(cycle))
                cycles.add(tuple(cycle[i:] + cycle[:i]))
        stack.pop()
        state[node] = 2

    for name in sorted(deps):
        if state.get(name, 0) == 0:
            visit(name, [])
    return sorted(cycles)


def validate_relations(flag_set: FlagSet, report: RejectionReport | None = None) -> RejectionReport:
    """校验 flag_set 中的全部组合关系，问题写入 report 并返回。"""
    if report is None:
        report = RejectionReport()

    # 1) 依赖目标必须存在
    for name in sorted(flag_set.flags):
        for dep in sorted(set(flag_set.flags[name].depends_on)):
            if dep not in flag_set.flags:
                report.add(SOURCE, name, RejectReason.INVALID_VALUE,
                           f"depends_on 引用了不存在的开关 {dep!r}")

    # 2) 依赖不得成环（含自依赖）
    for cycle in _find_cycles(flag_set):
        path = " -> ".join(cycle + (cycle[0],))
        report.add(SOURCE, cycle[0], RejectReason.INVALID_VALUE,
                   f"depends_on 构成依赖环：{path}")

    # 3) 互斥组内不得有两个及以上“无条件生效”的开关（静态必然冲突）
    for group in sorted(flag_set.group_index):
        members = flag_set.group_index[group]
        always_on = [m for m in members if _is_unconditionally_active(flag_set.flags[m])]
        if len(always_on) > 1:
            others = ", ".join(always_on)
            for name in always_on:
                report.add(SOURCE, name, RejectReason.INVALID_VALUE,
                           f"互斥组 {group!r} 中多个开关无条件生效（{others}），同一时刻只能有一个")
    return report
