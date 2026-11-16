"""Explicit registration keeps migrations linear as the agent count grows."""

from typing import Dict, List

from .adapters.base import SourceAdapter, TargetAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._sources: Dict[str, SourceAdapter] = {}
        self._targets: Dict[str, TargetAdapter] = {}

    def register_source(self, adapter: SourceAdapter) -> None:
        if adapter.name in self._sources:
            raise ValueError("Source adapter already registered: " + adapter.name)
        self._sources[adapter.name] = adapter

    def register_target(self, adapter: TargetAdapter) -> None:
        if adapter.name in self._targets:
            raise ValueError("Target adapter already registered: " + adapter.name)
        self._targets[adapter.name] = adapter

    def source(self, name: str) -> SourceAdapter:
        if name not in self._sources:
            raise ValueError("Unsupported source agent: " + name)
        return self._sources[name]

    def target(self, name: str) -> TargetAdapter:
        aliases = {"deepseekharness": "deepseek", "deepseek-harness": "deepseek", "dsh": "deepseek"}
        key = aliases.get(name, name)
        if key not in self._targets:
            raise ValueError("Unsupported target agent: " + name)
        return self._targets[key]

    def describe(self) -> Dict[str, List[str]]:
        return {"sources": sorted(self._sources), "targets": sorted(self._targets)}


def default_registry() -> AdapterRegistry:
    from .adapters.codex import CodexSource
    from .adapters.deepseek import DeepSeekTarget

    registry = AdapterRegistry()
    registry.register_source(CodexSource())
    registry.register_target(DeepSeekTarget())
    return registry
