import json
import logging

from app.config.errors import RejectionReport, RejectReason
from app.config.loader import load_env_source, load_file_source, mask_config, merge_config

log = logging.getLogger(__name__)


def reasons(report):
    return {(r.source.split(":")[0], r.key, r.reason) for r in report.rejections}


def test_merge_priority_env_over_file_over_defaults(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"max_connections": 50, "debug": True}))
    report = RejectionReport()
    file_values = load_file_source(str(cfg_file), report)
    env_values = load_env_source({"APP_MAX_CONNECTIONS": "200"}, report=report)
    config, rep = merge_config(file_values, env_values)
    log.info("输入 file=%s env=%s 最终配置=%s 拒绝=%s", file_values, env_values, config, rep.rejections)
    assert rep.ok
    assert config["max_connections"] == 200  # env 覆盖 file
    assert config["debug"] is True           # file 覆盖默认
    assert config["service_name"] == "feature-flags"  # 默认值
    assert config["sample_rate"] == 1.0


def test_type_mismatch_rejected(tmp_path):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"max_connections": "not-an-int"}))
    report = RejectionReport()
    file_values = load_file_source(str(cfg_file), report)
    config, rep = merge_config(file_values)
    log.info("输入 file=%s 拒绝=%s", file_values, rep.rejections)
    assert config is None
    assert ("file", "max_connections", RejectReason.TYPE_MISMATCH) in reasons(rep)


def test_env_type_mismatch_rejected():
    env_values = load_env_source({"APP_DEBUG": "maybe"})
    config, rep = merge_config(env_values=env_values)
    # load_env_source 自身的 report 需要显式传入才能拿到；这里重做一次
    report = RejectionReport()
    load_env_source({"APP_DEBUG": "maybe"}, report=report)
    assert ("env", "debug", RejectReason.TYPE_MISMATCH) in reasons(report)


def test_unknown_key_rejected(tmp_path):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"nope": 1}))
    report = RejectionReport()
    file_values = load_file_source(str(cfg_file), report)
    assert ("file", "nope", RejectReason.UNKNOWN_KEY) in reasons(report)
    config, rep = merge_config(file_values)
    assert config is not None  # 未知项已在加载期拒绝，合法子集可合并


def test_missing_required_rejected():
    config, rep = merge_config(file_values={"service_name": None})
    assert config is None
    assert ("merged", "service_name", RejectReason.MISSING_REQUIRED) in reasons(rep)


def test_sensitive_masked():
    config, rep = merge_config(env_values={})
    config["api_token"] = "secret-token"
    masked = mask_config(config)
    log.info("脱敏前 token=%s 脱敏后=%s", config["api_token"], masked["api_token"])
    assert masked["api_token"] == "***"
    assert masked["service_name"] == "feature-flags"
