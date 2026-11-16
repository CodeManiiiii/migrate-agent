"""Versioned interchange model. Adapters never depend on each other."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Message:
    role: str
    content: str
    timestamp: str = ""
    kind: str = "message"
    name: str = ""
    call_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    id: str
    title: str
    cwd: str
    messages: List[Message]
    created_at: str = ""
    updated_at: str = ""
    archived: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    records: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Document:
    path: str
    content: str
    kind: str = "instruction"
    scope: str = "global"
    project: str = ""
    encoding: str = "utf-8"
    executable: bool = False


@dataclass
class Bundle:
    source: str
    sessions: List[Session] = field(default_factory=list)
    documents: List[Document] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    source_root: str = ""
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        # Avoid dataclasses.asdict(): it deep-copies every raw history record,
        # which can double memory consumption for long-lived Codex stores.
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "source_root": self.source_root,
            "sessions": [
                {
                    "id": session.id, "title": session.title, "cwd": session.cwd,
                    "created_at": session.created_at, "updated_at": session.updated_at,
                    "archived": session.archived, "metadata": session.metadata,
                    "records": session.records,
                    "messages": [
                        {
                            "role": message.role, "content": message.content,
                            "timestamp": message.timestamp, "kind": message.kind,
                            "name": message.name, "call_id": message.call_id,
                            "metadata": message.metadata,
                        }
                        for message in session.messages
                    ],
                }
                for session in self.sessions
            ],
            "documents": [
                {
                    "path": document.path, "content": document.content,
                    "kind": document.kind, "scope": document.scope,
                    "project": document.project, "encoding": document.encoding,
                    "executable": document.executable,
                }
                for document in self.documents
            ],
            "settings": self.settings,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class SourceOptions:
    home: Path
    projects: Tuple[Path, ...] = ()
    session_ids: Tuple[str, ...] = ()
    include_archived: bool = True
    include_skills: bool = True
    include_project_docs: bool = True
    discover_projects: bool = True


@dataclass(frozen=True)
class TargetOptions:
    home: Path
    # Explicit source-to-target project mappings. Unmapped projects keep their cwd.
    project_map: Dict[str, str] = field(default_factory=dict)
    agents_home: Optional[Path] = None
    compression: str = "zstd"

    @property
    def agent_root(self) -> Path:
        return self.agents_home if self.agents_home is not None else self.home.parent / ".agents"


@dataclass
class PlannedFile:
    """A target-relative file. The engine owns conflict and backup handling."""

    path: str
    content: bytes
    description: str = ""
    root: str = "home"
    # A managed merge preserves unrelated target content. The engine still
    # backs it up and verifies the destination has not changed since preview.
    merge: bool = False
    mode: int = 0o600


@dataclass
class TargetPlan:
    files: List[PlannedFile] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    session_map: Dict[str, str] = field(default_factory=dict)
    instructions: List[str] = field(default_factory=list)
    project_roots: Dict[str, Path] = field(default_factory=dict)
