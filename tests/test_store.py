"""并发刷新与读取一致性、失败刷新回滚、版本规则。"""
from __future__ import annotations

import logging
import threading

import pytest

from app.errors import ConfigError
from app.flags import FlagEvaluator
from app.models import FieldSpec, MergedConfig, Setting

log = logging.getLogger("tests")

SPECS = {
    "v": FieldSpec("v", "int", required=True, min=0, max=1_000_000),
}


def _config(v: int) -> MergedConfig:
    return MergedConfig(
        settings={"v": Setting("v", v, f"source-v{v}")},
        sources=(f"source-v{v}",),
    )


def _make_service(tmp_path):
    from tests.conftest import base_config, make_service
    return make_service(base_config(tmp_path))


def test_failed_refresh_keeps_last_good(tmp_path):
    svc = _make_service(tmp_path)
    svc.load_initial({"db": {"host": "h1"}})
    v_before = svc.current_version()
    log.info("初始版本 v%s，输入非法刷新配置", v_before)

    with pytest.raises(ConfigError):
        svc.refresh({"db": {"port": "NaN"}})

    assert svc.current_version() == v_before
    view = svc.view()
    assert view["settings"]["db.host"]["value"] == "h1"
    events = svc.audit_entries()
    assert events[-1]["action"] == "reject" and events[-1]["success"] is False
    log.info("失败刷新后仍为 v%s；审计 action=%s",
             svc.current_version(), events[-1]["action"])


def test_version_monotonic_and_rollback_creates_new_version(tmp_path):
    svc = _make_service(tmp_path)
    v1 = svc.load_initial({"db": {"host": "h1"}})
    v2 = svc.refresh({"db": {"host": "h2"}})
    assert v2 == v1 + 1
    v3 = svc.rollback(v1, "回退到初始内容")
    assert v3 == v2 + 1  # 版本号不倒退
    assert svc.view()["settings"]["db.host"]["value"] == "h1"
    log.info("版本序列 v%s -> v%s -> 回滚v%s（内容=v%s）", v1, v2, v3, v1)


def test_rollback_unknown_version(tmp_path):
    svc = _make_service(tmp_path)
    svc.load_initial({"db": {"host": "h1"}})
    with pytest.raises(KeyError):
        svc.rollback(999)


def test_store_apply_and_snapshot_immutability():
    from app.store import ConfigStore
    store = ConfigStore()
    store.apply(_config(1), lambda v: FlagEvaluator({}, v))
    snap = store.snapshot()
    store.apply(_config(2), lambda v: FlagEvaluator({}, v))
    # 已取到的快照不受后续刷新影响：无撕裂读取
    assert snap.version == 1
    assert snap.config.value_of("v") == 1
    assert store.snapshot().config.value_of("v") == 2


def test_concurrent_refresh_and_reads_no_tearing(tmp_path):
    svc = _make_service(tmp_path)
    svc.load_initial({"db": {"host": "h0", "port": 5432}})

    errors: list[Exception] = []
    stop = threading.Event()

    def reader() -> None:
        # 每次读取必须看到一份完整自洽的配置：
        # host 存在且为字符串、port 是声明范围内的 int、版本号为正整数
        while not stop.is_set():
            try:
                snap = svc._store.snapshot()  # noqa: SLF001
                cfg = snap.config
                host = cfg.settings["db.host"].value
                port = cfg.settings["db.port"].value
                assert isinstance(host, str) and host.startswith("h")
                assert isinstance(port, int) and 1 <= port <= 65535
                assert snap.version == snap.evaluator._version  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return

    def writer(n: int) -> None:
        for i in range(n):
            # 奇偶交替：一份合法、一份非法（非法必须被拒绝，不影响线上）
            try:
                svc.refresh({"db": {"host": f"h{1000 + i}", "port": 6000 + i}})
            except ConfigError:
                pass
            try:
                svc.refresh({"db": {"port": "bad"}})
            except ConfigError:
                pass

    readers = [threading.Thread(target=reader) for _ in range(8)]
    writers = [threading.Thread(target=writer, args=(25,)) for _ in range(4)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    log.info("并发结束: 最终版本 v%s，历史版本数 %d，读取错误 %d",
             svc.current_version(), len(svc.history_versions()), len(errors))
    assert not errors, f"并发读取发现撕裂/错误: {errors[:3]}"
    # 每次成功刷新 +1；全部成功版本都在历史中，无空洞
    versions = svc.history_versions()
    assert versions == list(range(1, len(versions) + 1))
    assert svc.view()["settings"]["db.port"]["value"] in range(6000, 6025)


def test_audit_trail_is_append_only(tmp_path):
    svc = _make_service(tmp_path)
    svc.load_initial({"db": {"host": "h1"}})
    with pytest.raises(ConfigError):
        svc.refresh({"db": {"port": "bad"}})
    svc.refresh({"db": {"host": "h2"}})
    svc.rollback(1, "手动回退")
    actions = [e["action"] for e in svc.audit_entries()]
    assert actions == ["load", "reject", "refresh", "rollback"]
    seqs = [e["seq"] for e in svc.audit_entries()]
    assert seqs == list(range(1, 5))
    log.info("审计动作序列: %s", actions)
