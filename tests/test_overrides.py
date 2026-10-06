import logging

from app.config.schema import ValueType
from app.flags.evaluator import DecisionKind, evaluate
from app.flags.model import Flag, FlagSet, Rule
from app.runtime.overrides import OverrideManager, mask_events

log = logging.getLogger(__name__)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_flag_set() -> FlagSet:
    flag = Flag("checkout", ValueType.BOOL, default=False, rules=(
        Rule("all", percentage=100.0, value=True),
    ))
    secret = Flag("api_key", ValueType.STR, default="", sensitive=True)
    return FlagSet(version=1, flags={"checkout": flag, "api_key": secret})


def show(d, note=""):
    log.info("%s判定=%s 值=%r 原因=%s", note, d.kind.value, d.value, d.reason)
    return d


def test_override_beats_rule_evaluation():
    clock = FakeClock()
    om = OverrideManager(clock)
    fs = make_flag_set()
    om.set("checkout", False, ttl_seconds=60, actor="ops", reason="线上止血")
    d = show(evaluate(fs, "checkout", {}, overrides=om), "覆盖期间 ")
    assert d.kind is DecisionKind.OVERRIDE and d.value is False


def test_override_expires_and_falls_back_to_rules():
    clock = FakeClock()
    om = OverrideManager(clock)
    fs = make_flag_set()
    om.set("checkout", False, ttl_seconds=60, actor="ops")
    assert evaluate(fs, "checkout", {}, overrides=om).value is False
    clock.advance(61)  # 到期
    d = show(evaluate(fs, "checkout", {}, overrides=om), "到期后 ")
    assert d.kind is DecisionKind.RULE_MATCH and d.value is True
    actions = [e.action for e in om.events()]
    log.info("事件序列=%s", actions)
    assert actions == ["set", "expired"]


def test_override_revoke():
    clock = FakeClock()
    om = OverrideManager(clock)
    fs = make_flag_set()
    om.set("checkout", False, ttl_seconds=600, actor="ops")
    assert om.revoke("checkout", actor="ops") is True
    assert om.revoke("checkout", actor="ops") is False  # 已撤销，再撤返回 False
    d = show(evaluate(fs, "checkout", {}, overrides=om), "撤销后 ")
    assert d.kind is DecisionKind.RULE_MATCH and d.value is True
    assert [e.action for e in om.events()] == ["set", "revoked"]


def test_override_can_force_on_disabled_flag():
    clock = FakeClock()
    om = OverrideManager(clock)
    fs = FlagSet(version=1, flags={
        "off_flag": Flag("off_flag", ValueType.BOOL, enabled=False, default=False),
    })
    om.set("off_flag", True, ttl_seconds=30, actor="ops")
    d = show(evaluate(fs, "off_flag", {}, overrides=om), "强制打开 ")
    assert d.kind is DecisionKind.OVERRIDE and d.value is True


def test_override_on_missing_flag_not_applied():
    clock = FakeClock()
    om = OverrideManager(clock)
    fs = make_flag_set()
    om.set("ghost", True, ttl_seconds=30, actor="ops")
    d = evaluate(fs, "ghost", {}, overrides=om)
    assert d.kind is DecisionKind.FLAG_MISSING


def test_override_replaces_existing():
    clock = FakeClock()
    om = OverrideManager(clock)
    om.set("checkout", False, ttl_seconds=60, actor="ops")
    om.set("checkout", True, ttl_seconds=120, actor="ops2")
    active = om.active()
    assert len(active) == 1 and active[0].value is True and active[0].actor == "ops2"


def test_invalid_ttl_rejected():
    om = OverrideManager(FakeClock())
    try:
        om.set("checkout", True, ttl_seconds=0)
        assert False, "应当抛出 ValueError"
    except ValueError as exc:
        log.info("非法 ttl 拒绝：%s", exc)


def test_events_masked_for_sensitive_flags():
    clock = FakeClock()
    om = OverrideManager(clock)
    om.set("api_key", "sk-secret-123", ttl_seconds=60, actor="ops")
    om.set("checkout", False, ttl_seconds=60, actor="ops")
    public = mask_events(om.events(), sensitive_flags={"api_key"})
    log.info("脱敏后事件=%s", public)
    by_flag = {e["flag"]: e for e in public}
    assert by_flag["api_key"]["value"] == "***"
    assert by_flag["checkout"]["value"] is False


def test_expired_event_recorded_once():
    clock = FakeClock()
    om = OverrideManager(clock)
    om.set("checkout", True, ttl_seconds=10, actor="ops")
    clock.advance(20)
    om.active()
    om.active()
    om.events()
    assert [e.action for e in om.events()] == ["set", "expired"]
