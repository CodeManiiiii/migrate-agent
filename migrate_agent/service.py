"""Application boundary usable from a CLI, desktop app, or HTTP controller."""

from typing import Optional

from .engine import MigrationEngine, MigrationPlan
from .models import SourceOptions, TargetOptions
from .registry import AdapterRegistry, default_registry


class MigrationService:
    def __init__(self, registry: Optional[AdapterRegistry] = None) -> None:
        self.registry = registry if registry is not None else default_registry()
        self.engine = MigrationEngine()

    def prepare(
        self,
        source: str,
        target: str,
        source_options: SourceOptions,
        target_options: TargetOptions,
        conflict: str = "abort",
        strict: bool = False,
    ) -> MigrationPlan:
        reader = self.registry.source(source)
        writer = self.registry.target(target)
        bundle = reader.read(source_options)
        if source_options.session_ids and not bundle.sessions:
            raise ValueError("指定会话没有可读取的历史：" + ", ".join(source_options.session_ids))
        target_plan = writer.plan(bundle, target_options)
        if strict and (bundle.warnings or target_plan.warnings):
            warnings = list(dict.fromkeys(bundle.warnings + target_plan.warnings))
            raise ValueError("Strict mode stopped migration: " + "; ".join(warnings))
        return self.engine.prepare(bundle, target_plan, target_options, conflict=conflict)
