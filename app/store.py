"""线程安全的配置快照存储。

- 读路径只在锁内取一次不可变快照引用，随后完全无锁：
  同一请求拿到快照后，期间即使发生刷新，也只会看到一份完整配置，
  不会出现部分生效或撕裂读取。
- 写路径（apply/rollback）在同一把锁内完成"替换引用 + 记录历史"，
  并发刷新被串行化；调用方必须先在锁外构建完整的新配置，
  再调用 apply，因此不存在半应用状态。
- 版本号严格单调 +1；回滚是"明确回退内容、版本继续前进"，
  版本号始终是可靠的全序，回退目标记入审计原因。
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Callable

from app.flags import FlagEvaluator
from app.models import MergedConfig

EvaluatorFactory = Callable[[int], FlagEvaluator]


@dataclass(frozen=True)
class Snapshot:
    config: MergedConfig
    evaluator: FlagEvaluator
    version: int


class ConfigStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._current: Snapshot | None = None
        self._history: dict[int, Snapshot] = {}

    def snapshot(self) -> Snapshot:
        with self._lock:
            if self._current is None:
                raise RuntimeError("配置尚未初始化，请先调用 load_initial")
            return self._current

    def is_initialized(self) -> bool:
        with self._lock:
            return self._current is not None

    def apply(
        self,
        config: MergedConfig,
        evaluator_factory: EvaluatorFactory,
        reason: str | None = None,
    ) -> int:
        """原子发布一份完整的新配置，返回新版本号。

        evaluator_factory(version) 在锁内以最终版本号构造判定器，
        保证快照内配置与判定器版本严格一致。
        """
        with self._lock:
            old_version = self._current.version if self._current else 0
            new_version = old_version + 1
            snap = Snapshot(
                config=config,
                evaluator=evaluator_factory(new_version),
                version=new_version,
            )
            self._history[new_version] = snap
            self._current = snap
            return new_version

    def rollback(self, target_version: int, reason: str | None = None) -> int:
        """明确回滚到历史版本内容；生成新版本号，版本号不倒退。"""
        with self._lock:
            if target_version not in self._history:
                raise KeyError(f"历史版本不存在: v{target_version}")
            old = self._current
            if old is not None and target_version == old.version:
                raise ValueError(f"v{target_version} 即当前版本，无需回滚")
            target = self._history[target_version]
            new_version = (old.version if old else 0) + 1
            snap = Snapshot(
                config=target.config,
                evaluator=target.evaluator.with_version(new_version),
                version=new_version,
            )
            self._history[new_version] = snap
            self._current = snap
            return new_version

    def current_version(self) -> int:
        with self._lock:
            return self._current.version if self._current else 0

    def history_versions(self) -> list[int]:
        with self._lock:
            return sorted(self._history)

    def snapshot_of(self, version: int) -> Snapshot:
        with self._lock:
            return self._history[version]
