import logging
import threading
import time

from app.config.errors import RejectionReport
from app.config.schema import SCHEMA, ValueType
from app.flags.evaluator import DecisionKind, evaluate
from app.flags.model import Flag, FlagSet
from app.runtime.store import ConfigStore, Snapshot

log = logging.getLogger(__name__)


def bf(name: str, **kw) -> Flag:
    kw.setdefault("default", False)
    return Flag(name, ValueType.BOOL, **kw)


def make_store() -> ConfigStore:
    config = {f.name: f.default for f in SCHEMA}
    flags = FlagSet(version=1, flags={"old_flag": bf("old_flag", default=True)})
    return ConfigStore(Snapshot(version=1, config=config, flags=flags))


def test_preflight_diff_summary():
    store = make_store()
    current = store.current()
    new_config = dict(current.config, max_connections=200, api_token="tok-secret")
    new_flags = FlagSet(version=2, flags={
        "old_flag": bf("old_flag", default=False),   # 改动
        "new_flag": bf("new_flag"),                  # 新增
    })
    pre = store.preflight(new_config, new_flags, RejectionReport())
    log.info("预检摘要=%s 开关差异=%s 配置差异=%s", pre.summary, pre.flag_diffs, pre.config_diffs)
    assert pre.ok
    flag_diffs = {(d.kind, d.name) for d in pre.flag_diffs}
    assert ("added", "new_flag") in flag_diffs
    assert ("changed", "old_flag") in flag_diffs
    config_changed = {d.name for d in pre.config_diffs if d.kind == "changed"}
    assert "max_connections" in config_changed
    # 敏感字段在摘要中脱敏
    token_diff = next(d for d in pre.config_diffs if d.name == "api_token")
    assert "tok-secret" not in token_diff.detail and "***" in token_diff.detail


def test_preflight_cycle_blocks_refresh_and_state_untouched():
    store = make_store()
    before = store.current()
    bad_flags = FlagSet(version=2, flags={
        "a": bf("a", depends_on=("b",)),
        "b": bf("b", depends_on=("a",)),
    })
    result = store.refresh(dict(before.config), bad_flags, RejectionReport(), actor="ops")
    log.info("拒绝详情=%s 拒绝记录=%s", result.detail, result.report.rejections)
    assert not result.ok
    assert result.preflight is not None and not result.preflight.ok
    assert any("依赖环" in r.detail for r in result.report.rejections)
    # 线上状态一点不变：同一快照对象、版本不变、审计只有拒绝记录
    assert store.current() is before
    assert store.current().version == 1
    assert store.audit_log()[-1].action == "refresh_rejected"


def test_preflight_group_conflict_blocks_refresh():
    store = make_store()
    before = store.current()
    bad_flags = FlagSet(version=2, flags={
        "x": bf("x", enabled=True, default=True, exclusive_group="g"),
        "y": bf("y", enabled=True, default=True, exclusive_group="g"),
    })
    result = store.refresh(dict(before.config), bad_flags, RejectionReport())
    log.info("组冲突拒绝=%s", [r.detail for r in result.report.rejections])
    assert not result.ok
    assert any("互斥组" in r.detail for r in result.report.rejections)
    assert store.current() is before


def test_preflight_invalid_flag_value_blocks_refresh():
    store = make_store()
    before = store.current()
    bad_flags = FlagSet(version=2, flags={
        "rate": Flag("rate", ValueType.INT, default="not-an-int"),
    })
    result = store.refresh(dict(before.config), bad_flags, RejectionReport())
    assert not result.ok
    assert any(r.key == "rate" for r in result.report.rejections)
    assert store.current() is before


def test_preflight_config_rejection_blocks_refresh():
    store = make_store()
    before = store.current()
    report = RejectionReport()
    report.add("file", "debug", "type_mismatch", "期望 bool")
    result = store.refresh(None, FlagSet(version=2, flags={}), report)
    assert not result.ok
    assert store.current() is before


def test_refresh_success_with_preflight_summary_in_audit():
    store = make_store()
    new_flags = FlagSet(version=2, flags={"old_flag": bf("old_flag", default=True),
                                          "extra": bf("extra")})
    result = store.refresh(dict(store.current().config), new_flags, RejectionReport())
    assert result.ok
    audit = store.audit_log()[-1]
    log.info("审计记录=%s", audit)
    assert audit.action == "refresh" and "开关" in audit.detail


def test_large_flag_set_concurrent_evaluation_stable():
    # 1000 个开关（含互斥组与依赖），并发求值结果一致且无锁竞争退化
    flags = {}
    for i in range(1000):
        flags[f"flag_{i:04d}"] = bf(f"flag_{i:04d}", default=True)
    for i in range(0, 200, 2):  # 100 个互斥组，每组 2 个
        flags[f"flag_{i:04d}"] = bf(f"flag_{i:04d}", default=True, exclusive_group=f"g{i//2}")
        flags[f"flag_{i+1:04d}"] = bf(f"flag_{i+1:04d}", default=True, exclusive_group=f"g{i//2}")
    for i in range(200, 400):  # 200 条依赖链
        flags[f"flag_{i:04d}"] = bf(f"flag_{i:04d}", default=True, depends_on=("flag_0000",))
    fs = FlagSet(version=1, flags=flags)
    store = ConfigStore(Snapshot(version=1, config={}, flags=fs))

    results, errors = [], []

    def worker():
        try:
            for _ in range(20):
                snap = store.current()
                for name in ("flag_0000", "flag_0001", "flag_0200", "flag_0999"):
                    results.append(evaluate(snap.flags, name, {}).kind)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    start = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    log.info("16 线程 x 20 轮 x 4 开关 共 %d 次求值耗时 %.3fs", len(results), elapsed)
    assert not errors
    assert len(results) == 16 * 20 * 4
    # 互斥组裁决确定：flag_0000 永远生效，flag_0001 永远被排除
    assert all(k is DecisionKind.DEFAULT for r in results[0::4] for k in [r])
    assert all(k is DecisionKind.GROUP_EXCLUDED for r in results[1::4] for k in [r])
    assert elapsed < 5.0  # 粗略防退化上限（实际应在毫秒级）
