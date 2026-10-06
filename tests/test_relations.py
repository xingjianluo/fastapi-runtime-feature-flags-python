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
