import logging

from app.config.schema import ValueType
from app.flags.model import Flag, FlagSet
from app.flags.relations import validate_relations

log = logging.getLogger(__name__)


def fs(*flags: Flag) -> FlagSet:
    return FlagSet(version=1, flags={f.name: f for f in flags})


def bf(name: str, **kw) -> Flag:
    kw.setdefault("default", False)
    return Flag(name, ValueType.BOOL, **kw)


def test_no_relations_backward_compatible():
    report = validate_relations(fs(bf("a"), bf("b")))
    log.info("无关系声明 拒绝=%s", report.rejections)
    assert report.ok


def test_unknown_dependency_rejected():
    report = validate_relations(fs(bf("a", depends_on=("ghost",))))
    log.info("未知依赖 拒绝=%s", report.rejections)
    assert not report.ok
    r = report.rejections[0]
    assert r.key == "a" and "ghost" in r.detail


def test_dependency_cycle_rejected_regardless_of_declaration_order():
    # 同一组环，两种声明顺序，拒绝结论必须一致
    flags1 = [bf("a", depends_on=("b",)), bf("b", depends_on=("c",)), bf("c", depends_on=("a",))]
    flags2 = [bf("c", depends_on=("a",)), bf("a", depends_on=("b",)), bf("b", depends_on=("c",))]
    r1 = validate_relations(fs(*flags1))
    r2 = validate_relations(fs(*flags2))
    log.info("环顺序1=%s 环顺序2=%s", r1.rejections, r2.rejections)
    assert not r1.ok and not r2.ok
    assert [(r.key, r.detail) for r in r1.rejections] == [(r.key, r.detail) for r in r2.rejections]
    assert "a -> b -> c -> a" in r1.rejections[0].detail


def test_self_dependency_rejected():
    report = validate_relations(fs(bf("a", depends_on=("a",))))
    log.info("自依赖 拒绝=%s", report.rejections)
    assert not report.ok
    assert "a -> a" in report.rejections[0].detail


def test_exclusive_group_static_conflict_rejected():
    flags = [
        bf("new_ui", enabled=True, default=True, exclusive_group="ui"),
        bf("legacy_ui", enabled=True, default=True, exclusive_group="ui"),
        bf("other", enabled=True, default=True),
    ]
    report = validate_relations(fs(*flags))
    log.info("组内冲突 拒绝=%s", report.rejections)
    assert not report.ok
    keys = {r.key for r in report.rejections}
    assert keys == {"new_ui", "legacy_ui"}
    assert all("ui" in r.detail for r in report.rejections)


def test_exclusive_group_single_always_on_ok():
    flags = [
        bf("new_ui", enabled=True, default=True, exclusive_group="ui"),
        bf("legacy_ui", enabled=True, default=False, exclusive_group="ui"),
    ]
    report = validate_relations(fs(*flags))
    log.info("组内单开 拒绝=%s", report.rejections)
    assert report.ok


def test_diamond_dependency_ok():
    flags = [
        bf("d"),
        bf("b", depends_on=("d",)),
        bf("c", depends_on=("d",)),
        bf("a", depends_on=("b", "c")),
    ]
    report = validate_relations(fs(*flags))
    log.info("菱形依赖 拒绝=%s", report.rejections)
    assert report.ok


# ---------- 求值期语义 ----------

from app.flags.evaluator import DecisionKind, evaluate  # noqa: E402


def show(d, ctx):
    log.info("输入 flag=%s context=%s -> 判定=%s 值=%r 原因=%s",
             d.flag, ctx, d.kind.value, d.value, d.reason)
    return d


def test_dependency_satisfied_allows_evaluation():
    flags = fs(bf("base", enabled=True, default=True),
               bf("feature", depends_on=("base",), default=True))
    d = show(evaluate(flags, "feature", {}), {})
    assert d.kind is DecisionKind.DEFAULT and d.value is True


def test_dependency_unsatisfied_forces_off():
    flags = fs(bf("base", enabled=False, default=True),
               bf("feature", depends_on=("base",), default=True))
    d = show(evaluate(flags, "feature", {}), {})
    assert d.kind is DecisionKind.DEPENDENCY_UNSATISFIED
    assert "base" in d.reason


def test_dependency_chain_transitive():
    flags = fs(
        bf("a", enabled=True, default=True),
        bf("b", depends_on=("a",), default=True),
        bf("c", depends_on=("b",), default=True),
    )
    assert evaluate(flags, "c", {}).value is True
    # 关掉最底层，整条链都不生效
    flags_off = fs(
        bf("a", enabled=False, default=True),
        bf("b", depends_on=("a",), default=True),
        bf("c", depends_on=("b",), default=True),
    )
    d = show(evaluate(flags_off, "c", {}), {})
    assert d.kind is DecisionKind.DEPENDENCY_UNSATISFIED


def test_exclusive_group_winner_deterministic_regardless_of_order():
    # 两个开关按规则都生效时，名字最小者胜出，与声明顺序无关
    def build(order):
        members = {
            "beta": bf("beta", enabled=True, default=True, exclusive_group="g"),
            "alpha": bf("alpha", enabled=True, default=True, exclusive_group="g"),
        }
        return fs(*(members[n] for n in order))

    for order in (("alpha", "beta"), ("beta", "alpha")):
        flags = build(order)
        da = show(evaluate(flags, "alpha", {}), {})
        db = show(evaluate(flags, "beta", {}), {})
        assert da.kind is DecisionKind.DEFAULT and da.value is True  # alpha 胜出
        # 被排除：kind 标记为 GROUP_EXCLUDED（不计入“生效”），值回退该开关默认值
        assert db.kind is DecisionKind.GROUP_EXCLUDED
        assert db.value == flags.flags["beta"].default
        assert "alpha" in db.reason


def test_exclusive_group_loser_off_allows_other():
    flags = fs(
        bf("alpha", enabled=False, default=True, exclusive_group="g"),
        bf("beta", enabled=True, default=True, exclusive_group="g"),
    )
    d = show(evaluate(flags, "beta", {}), {})
    assert d.kind is DecisionKind.DEFAULT and d.value is True


def test_group_arbitration_uses_rule_evaluation():
    # 组内成员按规则对不同用户生效，裁决仍确定
    from app.flags.model import Condition, Operator, Rule
    vip_rule = Rule("vip", conditions=(Condition("tier", Operator.EQ, "vip"),), value=True)
    flags = fs(
        bf("alpha", enabled=True, default=False, rules=(vip_rule,), exclusive_group="g"),
        bf("beta", enabled=True, default=True, exclusive_group="g"),
    )
    ctx_vip = {"tier": "vip"}
    da = show(evaluate(flags, "alpha", ctx_vip), ctx_vip)
    db = show(evaluate(flags, "beta", ctx_vip), ctx_vip)
    assert da.value is True and da.kind is DecisionKind.RULE_MATCH
    assert db.kind is DecisionKind.GROUP_EXCLUDED
    ctx_free = {"tier": "free"}
    db2 = show(evaluate(flags, "beta", ctx_free), ctx_free)
    assert db2.value is True  # alpha 对该用户不生效，beta 正常生效


def test_no_relations_old_semantics_unchanged():
    flags = fs(bf("plain", enabled=True, default=True))
    d = evaluate(flags, "plain", {})
    assert d.kind is DecisionKind.DEFAULT and d.value is True
