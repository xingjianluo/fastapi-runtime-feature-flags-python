"""预检与原子发布：变更摘要、预检不过不允许发布、读写不混杂、规模下求值稳定。"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.config.schema import ValueType
from app.flags.evaluator import DecisionKind
from app.flags.model import Flag, FlagSet
from app.runtime.preflight import diff_snapshot, preflight
from app.runtime.store import ConfigStore, Snapshot

log = logging.getLogger(__name__)
B = ValueType.BOOL


def make_store():
    flags = FlagSet(1, {
        "keep": Flag("keep", B, default=True),
        "drop": Flag("drop", B),
        "chg": Flag("chg", B, default=False),
    })
    return ConfigStore(Snapshot(1, {"a": 1, "b": 2}, flags))


def candidate_flags():
    return FlagSet(2, {
        "keep": Flag("keep", B, default=True),
        "chg": Flag("chg", B, default=True),          # 改动
        "new": Flag("new", B, depends_on=("keep",)),  # 新增
    })


def test_diff_summary(caplog):
    caplog.set_level(logging.INFO)
    store = make_store()
    summary = diff_snapshot(store.current(), {"a": 1, "c": 3}, candidate_flags())
    log.info("预检摘要: flags +%s -%s ~%s config +%s -%s ~%s",
             summary.flags_added, summary.flags_removed, summary.flags_changed,
             summary.config_added, summary.config_removed, summary.config_changed)
    assert summary.flags_added == ("new",)
    assert summary.flags_removed == ("drop",)
    assert summary.flags_changed == ("chg",)
    assert summary.config_added == ("c",)
    assert summary.config_removed == ("b",)
    assert summary.config_changed == ()


def test_preflight_failure_blocks_publish_and_state_untouched(caplog):
    caplog.set_level(logging.INFO)
    store = make_store()
    before = store.current()
    bad = FlagSet(2, {
        "x": Flag("x", B, depends_on=("y",)),
        "y": Flag("y", B, depends_on=("x",)),
    })
    result = preflight(before, None, bad)
    log.info("预检 ok=%s 拒绝=%s", result.ok,
             [(r.key, r.reason.value) for r in result.report.rejections])
    assert not result.ok

    refresh = store.refresh(None, bad, actor="ops")
    assert not refresh.ok
    after = store.current()
    assert after is before  # 线上状态一点没变
    assert after.version == 1
    assert store.evaluate_flag("keep", {}).value is True
    log.info("预检/刷新均拒绝，当前版本仍为 v%s，审计=%s",
             after.version, [a.action for a in store.audit_log()])


def test_refresh_is_atomic_under_concurrent_reads(caplog):
    caplog.set_level(logging.INFO)
    import threading
    store = make_store()
    new_flags = candidate_flags()
    new_config = {"a": 10, "b": 20}
    start = threading.Event()
    observed = []

    def reader():
        start.wait()
        seen = []
        while 2 not in seen:  # 读到新版本为止，保证覆盖切换前后两种状态
            snap = store.current()
            # 单个快照内版本、配置、开关必须自洽，不允许新旧混杂
            if snap.version == 1:
                assert snap.config == {"a": 1, "b": 2}
                assert "new" not in snap.flags.flags
            else:
                assert snap.config == new_config
                assert "new" in snap.flags.flags
            seen.append(snap.version)
        observed.extend(seen)

    def writer():
        start.wait()
        time.sleep(0.01)  # 让读者先读到旧版本
        return store.refresh(new_config, new_flags, None, "ops")

    with ThreadPoolExecutor(max_workers=9) as pool:
        readers = [pool.submit(reader) for _ in range(8)]
        writer_fut = pool.submit(writer)
        start.set()
        result = writer_fut.result()
        for r in readers:
            r.result()
    assert result.ok and result.version == 2
    assert set(observed) == {1, 2}
    log.info("并发读到版本分布: v1=%d 次 v2=%d 次，无新旧混杂",
             observed.count(1), observed.count(2))


def test_evaluation_scales_with_many_flags(caplog):
    caplog.set_level(logging.INFO)
    count = 1000
    flags = {f"f{i}": Flag(f"f{i}", B, default=(i % 2 == 0)) for i in range(count)}
    # 链式依赖 + 互斥组混排
    for i in range(1, 200):
        flags[f"f{i}"] = Flag(f"f{i}", B, default=True, depends_on=(f"f{i-1}",))
    for i in range(200, 400, 2):
        flags[f"f{i}"] = Flag(f"f{i}", B, default=True, mutex_group=f"g{i//50}", priority=i)
        flags[f"f{i+1}"] = Flag(f"f{i+1}", B, default=True, mutex_group=f"g{i//50}", priority=i + 1)
    fs = FlagSet(1, flags)
    store = ConfigStore(Snapshot(1, {}, fs))

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(
            lambda i: store.evaluate_flag(f"f{i % count}", {"id": f"u{i}"}),
            range(5000),
        ))
    elapsed = time.perf_counter() - start
    kinds = {r.kind for r in results}
    log.info("5000 次并发求值（%d 个开关）耗时 %.3fs，判定分布=%s",
             count, elapsed, {k.value: sum(1 for r in results if r.kind is k) for k in kinds})
    assert all(r.kind is not DecisionKind.FLAG_MISSING for r in results)
    assert elapsed < 5.0  # 宽松上限，防止随条数出现明显退化
