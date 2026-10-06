"""临时覆盖：到期前/后行为、优先级高于规则、撤销与审计、敏感脱敏。"""
import logging

from app.config.loader import MASK
from app.config.schema import ValueType
from app.flags.evaluator import DecisionKind
from app.flags.model import Flag, FlagSet, Rule
from app.flags.overrides import Override, OverrideManager
from app.runtime.store import ConfigStore, Snapshot

log = logging.getLogger(__name__)
B = ValueType.BOOL


def make_store(now):
    flags = FlagSet(1, {
        "feat": Flag("feat", B, default=False,
                     rules=(Rule("always_on", (), 100.0, True),)),
        "secret": Flag("secret", B, default=False, sensitive=True),
    })
    mgr = OverrideManager(clock=lambda: now[0])
    return ConfigStore(Snapshot(1, {}, flags), overrides=mgr), mgr


def test_override_beats_rule_then_expires(caplog):
    caplog.set_level(logging.INFO)
    now = [1000.0]
    store, mgr = make_store(now)

    d = store.evaluate_flag("feat", {})
    assert d.kind is DecisionKind.RULE_MATCH and d.value is True
    log.info("覆盖前: kind=%s value=%s（规则命中）", d.kind.value, d.value)

    mgr.set_override(Override("feat", False, expires_at=1010.0,
                              created_at=now[0], actor="ops", reason="应急关闭"))
    d = store.evaluate_flag("feat", {})
    log.info("覆盖生效期: kind=%s value=%s source=%s", d.kind.value, d.value, d.reason)
    assert d.kind is DecisionKind.OVERRIDE and d.value is False

    now[0] = 1011.0  # 到期后自动恢复规则判定
    d = store.evaluate_flag("feat", {})
    log.info("到期后: kind=%s value=%s", d.kind.value, d.value)
    assert d.kind is DecisionKind.RULE_MATCH and d.value is True

    actions = [e.action for e in mgr.events()]
    log.info("审计事件: %s", actions)
    assert actions == ["set", "expired"]


def test_override_expiry_recorded_once_under_concurrency():
    now = [1000.0]
    store, mgr = make_store(now)
    mgr.set_override(Override("feat", False, expires_at=1005.0, created_at=now[0]))
    now[0] = 1006.0
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.evaluate_flag("feat", {}), range(64)))
    assert [e.action for e in mgr.events()] == ["set", "expired"]


def test_revoke_restores_rule_evaluation(caplog):
    caplog.set_level(logging.INFO)
    now = [1000.0]
    store, mgr = make_store(now)
    mgr.set_override(Override("feat", False, expires_at=2000.0, created_at=now[0]))
    assert store.evaluate_flag("feat", {}).kind is DecisionKind.OVERRIDE
    assert mgr.revoke("feat", actor="ops") is True
    assert mgr.revoke("feat") is False  # 已撤销，再次撤销返回 False
    d = store.evaluate_flag("feat", {})
    log.info("撤销后: kind=%s value=%s", d.kind.value, d.value)
    assert d.kind is DecisionKind.RULE_MATCH and d.value is True
    assert [e.action for e in mgr.events()] == ["set", "revoked"]


def test_sensitive_override_value_masked(caplog):
    caplog.set_level(logging.INFO)
    now = [1000.0]
    store, mgr = make_store(now)
    mgr.set_override(Override("secret", True, expires_at=2000.0,
                              created_at=now[0], actor="ops"))
    raw = mgr.events()
    masked = mgr.masked_events({"secret"})
    log.info("原始事件 value=%s 脱敏后 value=%s", raw[0].value, masked[0].value)
    assert raw[0].value is True
    assert masked[0].value == MASK
    # 非敏感开关不脱敏
    mgr.set_override(Override("feat", False, expires_at=2000.0, created_at=now[0]))
    assert mgr.masked_events({"secret"})[-1].value is False
