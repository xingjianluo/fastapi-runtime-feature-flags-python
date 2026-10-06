"""HTTP 入口：配置查看（脱敏）、开关求值、预检/刷新/回滚、临时覆盖。"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from app.config.errors import RejectionReport
from app.config.loader import mask_config, merge_config
from app.config.schema import SCHEMA
from app.flags.evaluator import Decision, evaluate
from app.flags.parse import parse_flag_set
from app.runtime.overrides import OverrideManager, mask_events
from app.runtime.preflight import PreflightReport
from app.runtime.store import ConfigStore


class EvaluateRequest(BaseModel):
    flag: str
    context: dict[str, Any] = {}


class PublishRequest(BaseModel):
    config: dict[str, Any] | None = None  # 缺省沿用当前配置
    flags: dict[str, Any] | None = None   # 缺省沿用当前开关集
    actor: str = "anonymous"


class RollbackRequest(BaseModel):
    version: int
    actor: str = "anonymous"


class OverrideRequest(BaseModel):
    flag: str
    value: Any
    ttl_seconds: float
    actor: str = "anonymous"
    reason: str = ""


def _decision_to_dict(d: Decision) -> dict[str, Any]:
    return {"flag": d.flag, "kind": d.kind.value, "value": d.value,
            "rule": d.rule, "reason": d.reason, "version": d.version}


def _rejections_to_list(report: RejectionReport) -> list[dict[str, Any]]:
    return [{"source": r.source, "key": r.key, "reason": r.reason.value, "detail": r.detail}
            for r in report.rejections]


def _preflight_to_dict(pre: PreflightReport) -> dict[str, Any]:
    return {
        "ok": pre.ok,
        "summary": pre.summary,
        "flag_diffs": [d.__dict__ for d in pre.flag_diffs],
        "config_diffs": [d.__dict__ for d in pre.config_diffs],
        "rejections": _rejections_to_list(pre.rejections),
    }


def create_app(store: ConfigStore, overrides: OverrideManager | None = None) -> FastAPI:
    app = FastAPI(title="runtime-feature-flags")
    app.state.store = store
    app.state.overrides = overrides or OverrideManager()

    def _prepare_publish(body: PublishRequest):
        """解析请求为 (config, flag_set, report)，供预检与刷新共用。"""
        current = store.current()
        report = RejectionReport()
        if body.config is None:
            config = current.config
        else:
            config, merge_report = merge_config(file_values=body.config, schema=SCHEMA)
            report.rejections.extend(merge_report.rejections)
        if body.flags is None:
            flag_set = current.flags
        else:
            flag_set, parse_report = parse_flag_set(
                {"flags": body.flags}, version=current.flags.version + 1)
            report.rejections.extend(parse_report.rejections)
        return config, flag_set, report

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        snap = store.current()
        return {"version": snap.version, "config": mask_config(snap.config, SCHEMA)}

    @app.post("/evaluate")
    def post_evaluate(body: EvaluateRequest) -> dict[str, Any]:
        snap = store.current()
        decision = evaluate(snap.flags, body.flag, body.context,
                            overrides=app.state.overrides)
        return _decision_to_dict(decision)

    @app.get("/audit")
    def get_audit() -> list[dict[str, Any]]:
        return [r.__dict__ for r in store.audit_log()]

    @app.post("/preflight")
    def post_preflight(body: PublishRequest) -> dict[str, Any]:
        config, flag_set, report = _prepare_publish(body)
        pre = store.preflight(config, flag_set, report)
        return _preflight_to_dict(pre)

    @app.post("/refresh")
    def post_refresh(body: PublishRequest) -> dict[str, Any]:
        config, flag_set, report = _prepare_publish(body)
        result = store.refresh(config, flag_set, report, actor=body.actor)
        return {"ok": result.ok, "version": result.version, "detail": result.detail,
                "preflight": _preflight_to_dict(result.preflight) if result.preflight else None}

    @app.post("/rollback")
    def post_rollback(body: RollbackRequest) -> dict[str, Any]:
        result = store.rollback(body.version, actor=body.actor)
        return {"ok": result.ok, "version": result.version, "detail": result.detail}

    @app.post("/overrides")
    def post_override(body: OverrideRequest) -> dict[str, Any]:
        override = app.state.overrides.set(body.flag, body.value, body.ttl_seconds,
                                           actor=body.actor, reason=body.reason)
        return {"flag": override.flag, "expires_at": override.expires_at}

    @app.delete("/overrides/{flag}")
    def delete_override(flag: str, actor: str = "anonymous") -> dict[str, Any]:
        revoked = app.state.overrides.revoke(flag, actor=actor)
        return {"flag": flag, "revoked": revoked}

    @app.get("/overrides")
    def get_overrides() -> dict[str, Any]:
        om: OverrideManager = app.state.overrides
        sensitive = {n for n, f in store.current().flags.flags.items() if f.sensitive}
        return {
            "active": [{"flag": o.flag, "expires_at": o.expires_at, "actor": o.actor,
                        "reason": o.reason} for o in om.active()],
            "events": mask_events(om.events(), sensitive_flags=sensitive),
        }

    return app
