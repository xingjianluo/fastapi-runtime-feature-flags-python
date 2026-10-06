import logging
import threading

from app.config.errors import RejectionReport, RejectReason
from app.config.schema import ValueType
from app.flags.model import Flag, FlagSet
from app.runtime.store import ConfigStore, Snapshot

log = logging.getLogger(__name__)


def make_store() -> ConfigStore:
    flags = FlagSet(version=1, flags={"a": Flag("a", ValueType.BOOL, default=False)})
    return ConfigStore(Snapshot(version=1, config={"service_name": "svc"}, flags=flags))


def good_flags(n: int) -> FlagSet:
    return FlagSet(version=n, flags={f"f{i}": Flag(f"f{i}", ValueType.BOOL, default=True)
                                     for i in range(n)})


def test_refresh_success_replaces_snapshot():
    store = make_store()
    result = store.refresh({"service_name": "svc2"}, good_flags(3), RejectionReport(), actor="ops")
    log.info("刷新结果 ok=%s version=%s", result.ok, result.version)
    assert result.ok and result.version == 2
    snap = store.current()
    assert snap.config["service_name"] == "svc2"
    assert len(snap.flags.flags) == 3
    assert store.audit_log()[-1].action == "refresh"


def test_refresh_rejected_keeps_old_snapshot():
    store = make_store()
    before = store.current()
    report = RejectionReport()
    report.add("file", "debug", RejectReason.TYPE_MISMATCH, "期望 bool")
    result = store.refresh(None, good_flags(2), report, actor="ops")
    log.info("拒绝详情=%s 当前版本=%s", result.detail, store.current().version)
    assert not result.ok
    assert store.current() is before  # 旧快照原样保留
    assert store.audit_log()[-1].action == "refresh_rejected"


def test_rollback_to_history_version():
    store = make_store()
    store.refresh({"service_name": "v2"}, good_flags(2), RejectionReport())
    store.refresh({"service_name": "v3"}, good_flags(3), RejectionReport())
    result = store.rollback(1, actor="ops")
    log.info("回滚结果 ok=%s version=%s", result.ok, result.version)
    assert result.ok
    assert store.current().config["service_name"] == "svc"
    assert store.audit_log()[-1].action == "rollback"


def test_rollback_unknown_version_rejected():
    store = make_store()
    result = store.rollback(99)
    assert not result.ok
    assert store.current().version == 1


def test_concurrent_reads_during_refresh_see_consistent_snapshot():
    store = make_store()
    errors = []

    def reader():
        for _ in range(2000):
            snap = store.current()
            # 同一份快照内 config 与 flags 必须来自同一次发布
            n_flags = len(snap.flags.flags)
            expected = {1: "svc", 2: "svc2"}.get(snap.version)
            if expected is None or snap.config["service_name"] != expected:
                errors.append((snap.version, n_flags, snap.config))
            if snap.version == 1 and n_flags != 1:
                errors.append(("mixed", snap.version, n_flags))
            if snap.version == 2 and n_flags != 5:
                errors.append(("mixed", snap.version, n_flags))

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for _ in range(50):
        store.refresh({"service_name": "svc2"}, good_flags(5), RejectionReport())
        store.rollback(1)
    for t in threads:
        t.join()
    log.info("并发读写完成，不一致次数=%d", len(errors))
    assert not errors
