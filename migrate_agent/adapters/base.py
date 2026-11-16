from typing import Protocol

from ..models import Bundle, SourceOptions, TargetOptions, TargetPlan


class SourceAdapter(Protocol):
    name: str

    def read(self, options: SourceOptions) -> Bundle:
        ...


class TargetAdapter(Protocol):
    name: str

    def plan(self, bundle: Bundle, options: TargetOptions) -> TargetPlan:
        ...
