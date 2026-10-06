"""组合关系：判定确定性、成环/冲突拒绝且旧配置不受影响、旧声明语义不变。"""
import logging
import random
from concurrent.futures import ThreadPoolExecutor

from app.config.schema import ValueType
from app.flags.evaluator import DecisionKind, evaluate
from app.flags.model import Flag, FlagSet
from app.flags.relations import validate_relations
from app.runtime.store import ConfigStore, Snapshot

log = logging.getLogger(__name__)
B = ValueType.BOOL


def make_flags():
    """base <- feat 依赖；v1/v2 互斥（v2 优先级高）。"""
    return {
        "base": Flag("base", B, default=True),
        "feat": Flag("feat", B, default=True, depends_on=("base",)),
        "v1": Flag("v1", B, default=True, mutex_group="exp", priority=1),
        "v2": Flag("v2", B, default=True, mutex_group="exp", priority=2),
    }


def test_decision_deterministic_regardless_of_declaration_order(caplog):
    caplog.set_level(logging.INFO)
    flags = make_flags()
    decisions_baseline = {}
    for seed in range(10):
        items = list(flags.items())
        random.Random(seed).shuffle(items)
        fs = FlagSet(1, dict(items))
        got = {name: (evaluate(fs, name, {}).kind, evaluate(fs, name, {}).value)
               for name in flags}
        if decisions_baseline:
            assert got == decisions_baseline, f"声明顺序影响判定: seed={seed}"
        decisions_baseline = got
    log.info("10 种声明顺序下判定完全一致: %s", decisions_baseline)
    assert decisions_baseline["v1"][0] is DecisionKind.MUTEX_LOST
    assert decisions_baseline["v2"][1] is True
    assert decisions_baseline["feat"][1] is True


def test_concurrent_evaluation_consistent(caplog):
    caplog.set_level(logging.INFO)
    fs = FlagSet(1, make_flags())
    contexts = [{"id": f"user-{i}"} for i in range(50)]

    def work(ctx):
        return {n: (evaluate(fs, n, ctx).kind.value, evaluate(fs, n, ctx).value)
                for n in fs.flags}
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(work, contexts * 4))
    for i, ctx in enumerate(contexts):
        copies = [results[i + 50 * k] for k in range(4)]
        assert all(c == copies[0] for c in copies), f"并发判定不一致: {ctx}"
    log.info("200 次并发求值（16 线程）结论一致，样例: %s", results[0])


def test_dependency_unsatisfied_forces_off(caplog):
    caplog.set_level(logging.INFO)
    fs = FlagSet(1, {
        "base": Flag("base", B, enabled=False),
        "feat": Flag("feat", B, default=True, depends_on=("base",)),
    })
    d = evaluate(fs, "feat", {})
    log.info("输入 base.enabled=False feat.depends_on=(base,) -> %s %s", d.kind.value, d.value)
    assert d.kind is DecisionKind.DEPENDENCY_UNSATISFIED
    assert d.value is False
    assert "base" in d.reason


def test_cycle_rejected_and_old_config_kept(caplog):
    caplog.set_level(logging.INFO)
    good = FlagSet(1, {"old": Flag("old", B, default=True)})
    store = ConfigStore(Snapshot(1, {"k": "v"}, good))
    bad = FlagSet(2, {
        "a": Flag("a", B, depends_on=("b",)),
        "b": Flag("b", B, depends_on=("a",)),
    })
    result = store.refresh(None, bad, actor="ops")
    log.info("成环声明刷新结果 ok=%s 拒绝=%s", result.ok,
             [(r.key, r.reason.value, r.detail) for r in result.report.rejections])
    assert not result.ok
    assert any(r.reason.value == "dependency_cycle" for r in result.report.rejections)
    # 拒绝记录指出了具体开关与关系
    assert any(r.key in ("a", "b") for r in result.report.rejections)
    snap = store.current()
    assert snap.version == 1 and set(snap.flags.flags) == {"old"}
    assert store.evaluate_flag("old", {}).value is True
    assert store.audit_log()[-1].action == "refresh_rejected"


def test_mutex_conflict_rejected_and_old_config_kept(caplog):
    caplog.set_level(logging.INFO)
    store = ConfigStore(Snapshot(1, {}, FlagSet(1, {"old": Flag("old", B)})))
    conflicted = FlagSet(2, {
        "v1": Flag("v1", B, mutex_group="exp", priority=1),
        "v2": Flag("v2", B, mutex_group="exp", priority=1),  # 并列优先级
    })
    result = store.refresh(None, conflicted)
    log.info("互斥组并列优先级刷新结果 ok=%s 拒绝=%s", result.ok,
             [(r.key, r.reason.value, r.detail) for r in result.report.rejections])
    assert not result.ok
    assert any(r.reason.value == "relation_conflict" and r.key == "v2"
               for r in result.report.rejections)
    assert store.current().version == 1
    assert set(store.current().flags.flags) == {"old"}


def test_unknown_dependency_rejected():
    fs = FlagSet(1, {"a": Flag("a", B, depends_on=("ghost",))})
    report = validate_relations(fs)
    assert not report.ok
    assert any(r.reason.value == "unknown_key" and "ghost" in r.detail
               for r in report.rejections)


def test_legacy_flags_without_relations_keep_old_semantics(caplog):
    caplog.set_level(logging.INFO)
    fs = FlagSet(1, {
        "plain": Flag("plain", B, default=True),
        "off": Flag("off", B, enabled=False, default=True),
    })
    assert validate_relations(fs).ok
    d1 = evaluate(fs, "plain", {})
    d2 = evaluate(fs, "off", {})
    log.info("旧式声明判定: plain=%s/%s off=%s/%s",
             d1.kind.value, d1.value, d2.kind.value, d2.value)
    assert (d1.kind, d1.value) == (DecisionKind.DEFAULT, True)
    assert (d2.kind, d2.value) == (DecisionKind.FLAG_DISABLED, False)
