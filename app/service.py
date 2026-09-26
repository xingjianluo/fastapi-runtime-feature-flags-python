"""配置服务：加载-校验-审计-发布-回滚的统一门面。

刷新协议：
1. 在锁外构建并完整校验新的 MergedConfig 与 FlagEvaluator；
2. 校验失败 -> 写 reject 审计，抛出 ConfigError，线上继续使用最近一次有效配置；
3. 校验成功 -> store.apply 原子替换（锁内单次引用赋值），写 refresh 审计。
因此失败刷新绝不产生半应用状态，并发刷新由 store 串行化。
"""
from __future__ import annotations

import logging
from typing import Any

from app.audit import AuditEvent, AuditLog
from app.errors import ConfigError, Issue
from app.flags import Decision, FlagEvaluator, FlagSpec
from app.merge import (
    _flatten,
    load_env_values,
    load_file_raw,
    merge_and_validate,
)
from app.models import FieldSpec, MergedConfig
from app.store import ConfigStore

logger = logging.getLogger("config_service")

MASK = "********"
FLAGS_KEY = "feature_flags"
OVERRIDE_SOURCE = "api:refresh"


class ConfigService:
    def __init__(
        self,
        setting_specs: dict[str, FieldSpec],
        defaults: dict[str, Any],
        config_path: str | None = None,
        env_prefix: str = "APP_",
        flag_section: str = FLAGS_KEY,
    ) -> None:
        self._specs = dict(setting_specs)
        self._defaults = dict(defaults)
        self._config_path = config_path
        self._env_prefix = env_prefix
        self._flag_section = flag_section
        self._store = ConfigStore()
        self._audit = AuditLog()

    # ---------- 构建 ----------

    def build_config(
        self,
        overrides: dict[str, Any] | None = None,
        override_source: str = OVERRIDE_SOURCE,
    ) -> tuple[MergedConfig, dict[str, FlagSpec], tuple[str, ...]]:
        """构建并完整校验一份新配置（不发布）。"""
        overrides = dict(overrides or {})
        flag_overrides = overrides.pop(self._flag_section, None)
        if flag_overrides is not None and not isinstance(flag_overrides, dict):
            raise ConfigError([Issue(
                override_source, self._flag_section, "type",
                f"{self._flag_section} 必须是对象",
            )])

        file_raw: dict[str, Any] = {}
        file_source = f"file:{self._config_path}" if self._config_path else "file:<none>"
        if self._config_path:
            file_raw, file_source = load_file_raw(self._config_path)

        env_values, env_names = load_env_values(self._env_prefix)

        file_settings = {
            k: v for k, v in _flatten(file_raw).items()
            if not k.startswith(f"{self._flag_section}.")
        }
        file_flags = file_raw.get(self._flag_section, {})
        if file_flags is not None and not isinstance(file_flags, dict):
            raise ConfigError([Issue(
                file_source, self._flag_section, "type",
                f"{self._flag_section} 必须是对象",
            )])

        # 开关声明合并：override > file（按整个 section 合并，逐键取高优先级）
        merged_flags: dict[str, Any] = dict(file_flags or {})
        if flag_overrides:
            merged_flags.update(flag_overrides)

        # 先严格解析全部开关声明，非法即整体拒绝
        flag_specs: dict[str, FlagSpec] = {}
        for flag_key in sorted(merged_flags):
            flag_specs[flag_key] = FlagSpec.from_raw(
                flag_key, merged_flags[flag_key],
                override_source if flag_overrides and flag_key in flag_overrides
                else file_source,
            )

        config = merge_and_validate(
            defaults=self._defaults,
            file_values=file_settings,
            file_source=file_source,
            env_values=env_values,
            env_names=env_names,
            specs=self._specs,
            override_values=_flatten(overrides) if overrides else None,
            override_source=override_source,
        )
        sources = tuple(sorted(set(config.sources) | {"flags"}))
        return config, flag_specs, sources

    # ---------- 脱敏 ----------

    def redact(self, raw: dict[str, Any]) -> dict[str, Any]:
        secrets = {k for k, spec in self._specs.items() if spec.secret}
        return _redact_tree(raw, secrets)

    @staticmethod
    def _mask() -> str:
        return MASK

    # ---------- 生命周期 ----------

    def load_initial(
        self,
        overrides: dict[str, Any] | None = None,
        override_source: str = "bootstrap",
    ) -> int:
        config, flag_specs, sources = self.build_config(
            overrides, override_source=override_source
        )
        version = self._store.apply(
            config,
            lambda v: FlagEvaluator(flag_specs, v),
            reason="initial",
        )
        self._audit.append(AuditEvent(
            seq=self._audit.next_seq(),
            at=AuditLog.now_iso(),
            action="load",
            old_version=None,
            new_version=version,
            success=True,
            reason=None,
            sources=sources,
        ))
        logger.info("config loaded version=%s sources=%s", version, sources)
        return version

    def _secret_paths(self) -> set[str]:
        return {k for k, spec in self._specs.items() if spec.secret}

    def _sanitize_config_error(self, exc: ConfigError) -> ConfigError:
        """敏感字段的错误详情不得携带原始值（审计/对外均安全）。"""
        secrets = self._secret_paths()
        safe: list[Issue] = []
        for issue in exc.issues:
            if issue.path in secrets:
                safe.append(Issue(
                    source=issue.source, path=issue.path, kind=issue.kind,
                    reason=f"{issue.kind} 校验失败（敏感字段，值已隐藏）",
                ))
            else:
                safe.append(issue)
        return ConfigError(safe)

    def refresh(self, overrides: dict[str, Any] | None = None) -> int:
        """在线刷新。任何校验失败都保留旧配置并记录 reject 审计。"""
        old_version = self._store.current_version()
        redacted_input = self.redact(overrides or {})
        logger.info(
            "refresh requested old_version=%s input=%s", old_version, redacted_input
        )
        try:
            config, flag_specs, sources = self.build_config(overrides)
        except ConfigError as exc:
            safe_exc = self._sanitize_config_error(exc)
            self._audit.append(AuditEvent(
                seq=self._audit.next_seq(),
                at=AuditLog.now_iso(),
                action="reject",
                old_version=old_version,
                new_version=None,
                success=False,
                reason=str(safe_exc),
                sources=(),
            ))
            logger.warning(
                "refresh rejected, keep v%s. issues=%s",
                old_version, safe_exc.explain(),
            )
            raise safe_exc from exc
        version = self._store.apply(
            config,
            lambda v: FlagEvaluator(flag_specs, v),
            reason="refresh",
        )
        self._audit.append(AuditEvent(
            seq=self._audit.next_seq(),
            at=AuditLog.now_iso(),
            action="refresh",
            old_version=old_version,
            new_version=version,
            success=True,
            reason=None,
            sources=sources,
        ))
        logger.info(
            "refresh applied v%s -> v%s sources=%s",
            old_version, version, sources,
        )
        return version

    def rollback(self, target_version: int, reason: str | None = None) -> int:
        old_version = self._store.current_version()
        version = self._store.rollback(target_version, reason=reason)
        self._audit.append(AuditEvent(
            seq=self._audit.next_seq(),
            at=AuditLog.now_iso(),
            action="rollback",
            old_version=old_version,
            new_version=version,
            success=True,
            reason=f"rollback to v{target_version}"
                   + (f": {reason}" if reason else ""),
            sources=(),
        ))
        logger.warning(
            "rollback v%s -> v%s (content of v%s): %s",
            old_version, version, target_version, reason,
        )
        return version

    # ---------- 读取（一致视图） ----------

    def decision(
        self,
        flag: str,
        identity: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Decision:
        snap = self._store.snapshot()  # 一次取完整快照
        d = snap.evaluator.evaluate(flag, identity=identity, attributes=attributes)
        logger.info(
            "decision flag=%s identity=%s attributes=%s => enabled=%s basis=%s version=%s",
            flag, identity, attributes, d.enabled, d.basis, d.version,
        )
        return d

    def view(self, *, include_secrets: bool = False) -> dict[str, Any]:
        """对外配置视图：默认脱敏，并标注每个值的来源。"""
        snap = self._store.snapshot()
        settings: dict[str, Any] = {}
        secrets = {k for k, spec in self._specs.items() if spec.secret}
        for key, setting in snap.config.settings.items():
            value = setting.value
            if key in secrets and not include_secrets:
                value = MASK
            settings[key] = {"value": value, "source": setting.source}
        return {
            "version": snap.version,
            "sources": list(snap.config.sources),
            "settings": settings,
            "flags": sorted(snap.evaluator.flag_specs()),
        }

    def audit_entries(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._audit.entries()]

    def current_version(self) -> int:
        return self._store.current_version()

    def history_versions(self) -> list[int]:
        return self._store.history_versions()


def _redact_tree(raw: Any, secret_paths: set[str], path: str = "") -> Any:
    """按完整路径递归脱敏；列表元素沿用父路径匹配。"""
    if isinstance(raw, dict):
        out: dict[str, Any] = {}
        for key, value in raw.items():
            child_path = f"{path}.{key}" if path else str(key)
            if child_path in secret_paths:
                out[key] = MASK
            else:
                out[key] = _redact_tree(value, secret_paths, child_path)
        return out
    if isinstance(raw, list):
        return [_redact_tree(v, secret_paths, path) for v in raw]
    return raw
