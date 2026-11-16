"""Read Codex data without changing its on-disk stores.

Codex's JSONL rollouts are the authoritative source when available.  Recent
desktop builds also project history into SQLite; that projection is used for
threads whose rollout is unavailable.  Mirrored UI events are never appended
to the native transcript a second time.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any, Iterator

from migrate_agent.models import Bundle, Document, Message, Session, SourceOptions


_UUID = re.compile(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")
_IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}
_METADATA_KEYS = ("id", "rollout_path", "cwd", "title", "name", "created_at", "updated_at", "created_at_ms", "updated_at_ms", "archived", "model", "model_provider", "cli_version", "git_branch", "git_sha", "git_origin_url", "history_mode", "agent_nickname", "agent_role")


def _without_own_project_section(content: str, path: Path, warnings: list[str]) -> str:
    """Don't import our target additions as fresh Codex source instructions."""
    start_marker = "<!-- migrate-agent:codex:start -->"
    end_marker = "<!-- migrate-agent:codex:end -->"
    while start_marker in content:
        start = content.find(start_marker)
        end = content.find(end_marker, start)
        if end == -1:
            warnings.append(f"Incomplete migration section in source project rule: {path}")
            return content
        content = content[:start] + content[end + len(end_marker):]
    return content.rstrip() + "\n" if content.strip() else ""


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _matches_project(cwd: str, projects: tuple[Path, ...]) -> bool:
    if not projects:
        return True
    if not cwd:
        return False
    return any(_contained(Path(cwd).expanduser(), Path(project).expanduser()) for project in projects)


def _files(root: Path, boundary: Path, warnings: list[str]) -> Iterator[Path]:
    if not root.exists():
        return
    if not _contained(root, boundary):
        warnings.append(f"Skipped symlink outside source root: {root}")
        return
    for directory, names, files in os.walk(root, followlinks=False):
        kept = []
        for name in names:
            candidate = Path(directory) / name
            if name in _IGNORED_DIRS or name.startswith(".system"):
                continue
            if candidate.is_symlink():
                warnings.append(f"Skipped symbolic-link directory: {candidate}")
                continue
            kept.append(name)
        names[:] = sorted(kept)
        for name in sorted(files):
            path = Path(directory) / name
            if name == ".DS_Store":
                continue
            if not _contained(path, boundary):
                warnings.append(f"Skipped symlink outside source root: {path}")
                continue
            yield path


def _iso(value: Any, milliseconds: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / (1000 if milliseconds else 1), timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return str(value)
    return str(value)


def _text(value: Any) -> str:
    """Extract textual content while retaining attachment references."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_text(item) for item in value) if part)
    if isinstance(value, dict):
        if value.get("type") in {"encrypted_content", "refusal"}:
            return str(value.get("refusal", ""))
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), (str, list)):
            return _text(value["content"])
        if "image_url" in value:
            url = value["image_url"]
            if isinstance(url, dict):
                url = url.get("url", "")
            if str(url).startswith("data:"):
                return "[Embedded image retained in source records]"
            return f"[Image: {url}]"
        if value.get("type") in {"image", "localImage", "input_image", "input_file", "file"}:
            return f"[Attachment: {value.get('path') or value.get('file_id') or value.get('filename') or value.get('type')}]"
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def _has_attachments(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_attachments(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"image", "localImage", "input_image", "input_file", "file", "image_url", "input_audio", "audio"} or "image_url" in value:
            return True
        return any(_has_attachments(item) for item in value.values() if isinstance(item, (dict, list)))
    return False


def _native_message(payload: dict[str, Any], timestamp: str) -> Message | None:
    kind = payload.get("type", "")
    metadata = {}
    for key in ("id", "phase", "status", "author", "recipient"):
        if key in payload:
            metadata[key] = payload[key]
    if _has_attachments(payload.get("content")):
        metadata["attachments_present"] = True
    if kind == "message":
        content = _text(payload.get("content", ""))
        if content:
            return Message(role=str(payload.get("role", "user")), content=content, timestamp=timestamp, metadata=metadata)
    elif kind in {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}:
        content = _text(payload.get("arguments", payload.get("input", payload.get("action", ""))))
        return Message(role="assistant", content=content, timestamp=timestamp, kind="tool_call", name=str(payload.get("name", kind)), call_id=str(payload.get("call_id", payload.get("id", ""))), metadata=metadata)
    elif kind in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
        return Message(role="tool", content=_text(payload.get("output", "")), timestamp=timestamp, kind="tool_result", call_id=str(payload.get("call_id", "")), metadata=metadata)
    elif kind == "agent_message":
        return Message(role="assistant", content=_text(payload.get("content", "")), timestamp=timestamp, kind="agent_message", metadata=metadata)
    elif kind == "reasoning":
        # Public summaries are portable. Encrypted reasoning is preserved only
        # as an opaque source record and cannot serve as target model context.
        content = _text(payload.get("summary", []))
        if content:
            return Message(role="assistant", content=content, timestamp=timestamp, kind="reasoning_summary", metadata=metadata)
    return None


def _ui_messages(item: dict[str, Any], timestamp: str) -> list[Message]:
    typ = str(item.get("type", ""))
    typ = typ[:1].lower() + typ[1:]
    metadata = {"codex_item_type": typ, "id": item.get("id", "")}
    if _has_attachments(item.get("content")):
        metadata["attachments_present"] = True
    if typ == "userMessage":
        return [Message(role="user", content=_text(item.get("content", "")), timestamp=timestamp, metadata=metadata)]
    if typ == "agentMessage":
        return [Message(role="assistant", content=_text(item.get("text", "")), timestamp=timestamp, metadata=metadata)]
    if typ == "reasoning":
        content = _text(item.get("summary", []))
        return [Message(role="assistant", content=content, kind="reasoning_summary", timestamp=timestamp, metadata=metadata)] if content else []
    if typ == "commandExecution":
        return [Message(role="assistant", content=_text(item.get("command", "")), name="shell", kind="tool_call", timestamp=timestamp, call_id=str(item.get("id", "")), metadata=metadata), Message(role="tool", content=_text(item.get("aggregatedOutput", "")), kind="tool_result", timestamp=timestamp, call_id=str(item.get("id", "")), metadata={**metadata, "exit_code": item.get("exitCode")})]
    if typ in {"mcpToolCall", "dynamicToolCall"}:
        name = ".".join(str(item[k]) for k in ("server", "namespace", "tool") if item.get(k))
        return [Message(role="assistant", content=_text(item.get("arguments", "")), name=name, kind="tool_call", timestamp=timestamp, call_id=str(item.get("id", "")), metadata=metadata), Message(role="tool", content=_text(item.get("result", item.get("contentItems", item.get("error", "")))), kind="tool_result", timestamp=timestamp, call_id=str(item.get("id", "")), metadata=metadata)]
    if typ == "fileChange":
        return [Message(role="tool", content=_text(item.get("changes", [])), name="file_change", kind="tool_result", timestamp=timestamp, metadata=metadata)]
    if typ == "contextCompaction":
        return [Message(role="system", content="Codex compacted the conversation at this point; earlier visible history is retained in this migration.", kind="summary", timestamp=timestamp, metadata=metadata)]
    if typ in {"webSearch", "imageView", "imageGeneration", "collabAgentToolCall", "subAgentActivity"}:
        return [Message(role="tool", content=_text(item), name=typ, kind="tool_result", timestamp=timestamp, metadata=metadata)]
    return []


@contextmanager
def _rollout_stream(path: Path):
    if path.suffix != ".zst":
        with path.open("r", encoding="utf-8") as stream:
            yield stream
        return
    try:
        from compression import zstd  # type: ignore[attr-defined]
    except ImportError:
        zstd = None
    if zstd is not None:
        with zstd.open(path, "rt", encoding="utf-8") as stream:
            yield stream
        return
    try:
        import zstandard
    except ImportError:
        zstandard = None
    if zstandard is not None:
        with path.open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as reader, io.TextIOWrapper(reader, encoding="utf-8") as stream:
            yield stream
        return
    executable = shutil.which("zstd")
    if not executable:
        raise RuntimeError("zstd decompression unavailable; install migrate-agent[codex] or the zstd command")
    # Arguments are never interpolated into a shell command.
    process = subprocess.Popen([executable, "-dc", "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    try:
        with io.TextIOWrapper(process.stdout, encoding="utf-8") as stream:
            yield stream
        assert process.stderr is not None
        error = process.stderr.read()
        if process.wait() != 0:
            raise RuntimeError("zstd decompression failed: " + error.decode("utf-8", errors="replace")[:300])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stderr is not None:
            process.stderr.close()


class _ReadOnlyConnection(sqlite3.Connection):
    """Own an isolated sidecar snapshot until its reader is closed."""

    _snapshot_directory = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._snapshot_directory is not None:
                self._snapshot_directory.cleanup()
                self._snapshot_directory = None


def _database(home: Path, stem: str, warnings: list[str]) -> sqlite3.Connection | None:
    paths = sorted(home.glob(stem + "_*.sqlite"), key=lambda p: int(re.search(r"_(\d+)\.sqlite$", p.name).group(1)) if re.search(r"_(\d+)\.sqlite$", p.name) else 0, reverse=True)
    for path in paths:
        if not _contained(path, home):
            warnings.append(f"Skipped database outside source root: {path}")
            continue
        connection = None
        snapshot_directory = None
        try:
            wal = path.with_name(path.name + "-wal")
            shm = path.with_name(path.name + "-shm")
            if any(sidecar.exists() and not _contained(sidecar, home) for sidecar in (wal, shm)):
                warnings.append(f"Skipped database with a sidecar outside source root: {path.name}")
                continue
            connect_path = path.resolve()
            parameters = "?mode=ro"
            if not wal.exists():
                # A copied, checkpointed WAL-mode database retains its WAL
                # header even without sidecars. SQLite otherwise tries to
                # create/open them, which fails in a read-only source folder.
                # Never use immutable when an actual WAL might contain turns.
                parameters += "&immutable=1"
            else:
                # Even mode=ro changes SQLite's existing -shm reader marks.
                # Isolate every WAL-backed reader, including live databases,
                # so no source bytes or sidecars are changed by migration.
                snapshot_directory = tempfile.TemporaryDirectory(prefix="migrate-agent-sqlite-")
                connect_path = Path(snapshot_directory.name) / path.name
                copied_wal = connect_path.with_name(connect_path.name + "-wal")

                def signature(candidate: Path):
                    state = candidate.stat()
                    return (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns)

                stable = False
                for _attempt in range(3):
                    try:
                        before = (signature(path), signature(wal))
                        shutil.copyfile(path, connect_path)
                        shutil.copyfile(wal, copied_wal)
                        after = (signature(path), signature(wal))
                        if before == after:
                            stable = True
                            break
                    except FileNotFoundError:
                        # Checkpoint/rotation can remove WAL during a copy.
                        # Retry within a strict bound; never ignore its turns.
                        continue
                if not stable:
                    raise OSError("source changed during all 3 SQLite snapshot attempts; retry after its active writer has stopped")
            connection = sqlite3.connect(connect_path.as_uri() + parameters, uri=True, factory=_ReadOnlyConnection)
            connection._snapshot_directory = snapshot_directory
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            # Opening is lazy on some SQLite builds; validate actual reading
            # here so an inaccessible newer database can fall back cleanly.
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            return connection
        except (sqlite3.Error, OSError) as error:
            if connection is not None:
                connection.close()
            elif snapshot_directory is not None:
                snapshot_directory.cleanup()
            warnings.append(f"Cannot read {path.name}: {error}")
    return None


class CodexSource:
    name = "codex"

    def read(self, options: SourceOptions) -> Bundle:
        home = Path(options.home).expanduser().resolve()
        if not home.is_dir():
            raise ValueError(f"Codex home does not exist: {home}")
        bundle = Bundle(source=self.name, source_root=str(home))
        metadata = self._metadata(home, bundle.warnings)
        names = self._titles(home, bundle.warnings)
        requested = set(options.session_ids)
        seen: set[str] = set()
        candidates: list[tuple[Path, bool]] = []
        for folder, archived in (("sessions", False), ("archived_sessions", True)):
            if archived and not options.include_archived:
                continue
            for path in _files(home / folder, home, bundle.warnings):
                if path.name.endswith((".jsonl", ".jsonl.zst")):
                    candidates.append((path, archived))
        # A path in the metadata may use a nonstandard layout; include it only
        # when it remains inside the explicitly selected Codex home.
        candidate_paths = {path for path, _ in candidates}
        for data in metadata.values():
            path = Path(str(data.get("rollout_path", ""))).expanduser()
            if not path.is_absolute():
                path = home / path
            if not path.exists() and path.with_suffix(path.suffix + ".zst").exists():
                path = path.with_suffix(path.suffix + ".zst")
            if path.is_file() and _contained(path, home) and path not in candidate_paths:
                candidates.append((path, bool(data.get("archived", False))))
                candidate_paths.add(path)
        # Prefer an uncompressed copy over a stale compressed sibling.
        candidates.sort(key=lambda pair: (pair[1], pair[0].name.endswith(".zst"), str(pair[0])))
        for path, archived in candidates:
            match = _UUID.search(path.name)
            hint = match.group(1) if match else ""
            if hint in seen or (requested and hint and hint not in requested):
                continue
            data = metadata.get(hint, {})
            if not options.include_archived and (archived or data.get("archived")):
                continue
            if data.get("cwd") and not _matches_project(str(data["cwd"]), options.projects):
                continue
            session = self._rollout(path, archived, data, bundle.warnings)
            if session is None or session.id in seen or (requested and session.id not in requested):
                continue
            actual = metadata.get(session.id, data)
            self._enrich(session, actual, names)
            if not options.include_archived and session.archived:
                continue
            if not _matches_project(session.cwd, options.projects):
                continue
            if session.messages:
                bundle.sessions.append(session)
                seen.add(session.id)
            else:
                # Preserve source records and let SQLite fill in the visible
                # history, if this is a paginated-only rollout.
                metadata.setdefault(session.id, {}).setdefault("_rollout_records", session.records)
        self._sqlite_history(home, metadata, names, seen, requested, options, bundle)
        for session_id, data in metadata.items():
            if session_id not in seen and (not requested or session_id in requested) and (options.include_archived or not data.get("archived")) and _matches_project(str(data.get("cwd", "")), options.projects):
                bundle.warnings.append(f"Session {session_id} has no readable conversation history; not silently migrated as an empty session")
        if requested:
            for session_id in sorted(requested - seen):
                bundle.warnings.append(f"Requested session was not readable in the selected scope: {session_id}")
        bundle.sessions.sort(key=lambda session: (session.created_at, session.id))
        for session in bundle.sessions:
            if session.metadata.get("attachments_present") or any(message.metadata.get("attachments_present") for message in session.messages):
                bundle.warnings.append(f"Session {session.id} contains image/file/audio attachments: only text placeholders and original source records are preserved; Codex local attachment files are not copied and target native multimodal continuation is not supported")
        document_options = self._project_options(home, options, bundle)
        project_configs = self._documents(home, document_options, bundle)
        self._settings(home, document_options, bundle, project_configs)
        return bundle

    def _project_options(self, home: Path, options: SourceOptions, bundle: Bundle) -> SourceOptions:
        """Discover concrete project roots from selected history for one-click use.

        Filesystem/user roots are never scanned implicitly. A repository marker
        bounds ancestor discovery; markerless projects need local agent files.
        Explicit --project selections retain their filtering and scan semantics.
        """
        if options.projects or not options.discover_projects or not options.include_project_docs:
            return options
        roots: set[Path] = set()
        for session in bundle.sessions:
            if not session.cwd or not Path(session.cwd).is_absolute():
                continue
            cwd = Path(session.cwd).expanduser()
            if not cwd.is_dir() or cwd.is_symlink():
                continue
            cwd = cwd.resolve()
            candidates = [cwd, *cwd.parents]
            root = None
            for candidate in candidates:
                if candidate == Path(candidate.anchor) or candidate == Path.home().resolve():
                    break
                if (candidate / ".git").exists():
                    root = candidate
                    break
            if root is None and any((cwd / marker).exists() for marker in (
                "AGENTS.md", "AGENTS.override.md", ".codex", ".agents/skills",
            )):
                root = cwd
            if root is None or root == Path(root.anchor) or root == Path.home().resolve():
                continue
            if _contained(root, home) or _contained(home, root):
                continue
            roots.add(root)
        return replace(options, projects=tuple(sorted(roots, key=str)))

    def _metadata(self, home: Path, warnings: list[str]) -> dict[str, dict[str, Any]]:
        connection = _database(home, "state", warnings)
        if connection is None:
            return {}
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            fields = [key for key in _METADATA_KEYS if key in columns]
            if "id" not in fields:
                return {}
            return {str(row["id"]): dict(row) for row in connection.execute("SELECT " + ",".join(fields) + " FROM threads")}
        except sqlite3.Error as error:
            warnings.append(f"Cannot read Codex thread metadata: {error}")
            return {}
        finally:
            connection.close()

    def _titles(self, home: Path, warnings: list[str]) -> dict[str, str]:
        result = {}
        path = home / "session_index.jsonl"
        if not path.is_file() or not _contained(path, home):
            return result
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict) and row.get("id"):
                            result[str(row["id"])] = str(row.get("thread_name", row.get("title", "")))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except (OSError, UnicodeError) as error:
            warnings.append(f"Cannot read session index: {error}")
        return result

    def _enrich(self, session: Session, data: dict[str, Any], names: dict[str, str]) -> None:
        title = data.get("name") or names.get(session.id) or data.get("title")
        if title:
            session.title = str(title)
        if data.get("cwd"):
            session.cwd = str(data["cwd"])
        for attr in ("created_at", "updated_at"):
            if data.get(attr + "_ms") is not None:
                setattr(session, attr, _iso(data[attr + "_ms"], milliseconds=True))
            elif data.get(attr) is not None:
                setattr(session, attr, _iso(data[attr]))
        session.archived = bool(data.get("archived", session.archived))
        session.metadata.update({key: value for key, value in data.items() if not key.startswith("_") and key not in {"id", "title", "name", "cwd", "rollout_path"}})

    def _rollout(self, path: Path, archived: bool, data: dict[str, Any], warnings: list[str]) -> Session | None:
        records: list[dict[str, Any]] = []
        native_entries: list[tuple[int, Message]] = []
        event_entries: list[tuple[int, Message]] = []
        projection_entries: list[tuple[int, Message]] = []
        seen_items: set[tuple[str, str, str]] = set()
        meta: dict[str, Any] = {}
        malformed = 0
        try:
            with _rollout_stream(path) as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        malformed += 1
                        continue
                    if not isinstance(record, dict):
                        malformed += 1
                        continue
                    records.append(record)
                    typ = record.get("type", "")
                    payload = record.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    timestamp = _iso(record.get("timestamp", ""))
                    if typ == "session_meta":
                        meta.update(payload)
                    elif typ == "response_item":
                        message = _native_message(payload, timestamp)
                        if message is not None:
                            identity = (str(payload.get("type", "")), str(payload.get("id", "")), message.content)
                            if not payload.get("id") or identity not in seen_items:
                                native_entries.append((len(records), message))
                                seen_items.add(identity)
                    elif typ == "compacted":
                        summary = _text(payload.get("message", ""))
                        if summary:
                            native_entries.append((len(records), Message(role="system", content=summary, timestamp=timestamp, kind="summary")))
                    elif typ == "event_msg":
                        subtype = payload.get("type")
                        if subtype in {"user_message", "agent_message"}:
                            event_entries.append((len(records), Message(role="user" if subtype == "user_message" else "assistant", content=_text(payload.get("message", "")), timestamp=timestamp)))
                        elif subtype == "item_completed" and isinstance(payload.get("item"), dict):
                            projection_entries.extend((len(records), message) for message in _ui_messages(payload["item"], timestamp))
        except Exception as error:
            warnings.append(f"Cannot read rollout {path.name}: {error}; attempting SQLite history fallback")
            return None
        if malformed:
            warnings.append(f"Ignored {malformed} malformed JSONL record(s) in {path.name}; complete records were retained")
        match = _UUID.search(path.name)
        session_id = str(meta.get("id") or data.get("id") or (match.group(1) if match else ""))
        if not session_id:
            warnings.append(f"Skipped rollout without session identity: {path.name}")
            return None
        if not any(message.role in {"user", "assistant"} and message.kind == "message" for _, message in native_entries):
            # Legacy event-only user messages can coexist with native tool
            # records. Retain both in source order, without adding UI tool
            # projections when native tool records already exist.
            fallback = event_entries or projection_entries
            if event_entries:
                native_entries.extend(fallback)
            elif projection_entries:
                native_entries = projection_entries
        messages = [message for _, message in sorted(native_entries, key=lambda entry: entry[0])]
        title = str(meta.get("title", ""))
        if not title:
            title = next((message.content[:120].replace("\n", " ") for message in messages if message.role == "user"), session_id)
        created = _iso(meta.get("timestamp", "")) or (messages[0].timestamp if messages else "")
        updated = _iso(records[-1].get("timestamp", "")) if records else created
        portable_meta = {key: meta[key] for key in ("forked_from_id", "parent_thread_id", "originator", "cli_version", "source", "thread_source", "agent_nickname", "agent_path", "model_provider", "history_mode", "git") if key in meta}
        portable_meta["rollout_file"] = path.name
        if any(message.metadata.get("attachments_present") for message in messages):
            portable_meta["attachments_present"] = True
        if meta.get("base_instructions"):
            portable_meta["base_instructions"] = meta["base_instructions"]
        return Session(id=session_id, title=title, cwd=str(meta.get("cwd", "")), messages=messages, created_at=created, updated_at=updated, archived=archived, metadata=portable_meta, records=records)

    def _sqlite_history(self, home: Path, metadata: dict[str, dict[str, Any]], names: dict[str, str], seen: set[str], requested: set[str], options: SourceOptions, bundle: Bundle) -> None:
        connection = _database(home, "thread_history", bundle.warnings)
        if connection is None:
            return
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "thread_items" not in tables:
                return
            columns = {row[1] for row in connection.execute("PRAGMA table_info(thread_items)")}
            existing_sessions = {session.id: session for session in bundle.sessions}
            ids = [str(row[0]) for row in connection.execute("SELECT DISTINCT thread_id FROM thread_items")]
            for session_id in ids:
                if requested and session_id not in requested:
                    continue
                data = metadata.get(session_id, {})
                existing = existing_sessions.get(session_id)
                if not options.include_archived and data.get("archived"):
                    continue
                if not _matches_project(existing.cwd if existing else str(data.get("cwd", "")), options.projects):
                    continue
                if existing:
                    # Paginated builds can leave only a stub rollout while
                    # retaining older turns in SQLite. Count visible messages
                    # before trusting a nonempty rollout as a complete history.
                    if "item_type" in columns:
                        counts = {str(row[0]): int(row[1]) for row in connection.execute("SELECT item_type,COUNT(*) FROM thread_items WHERE thread_id=? AND item_type IN ('userMessage','agentMessage') GROUP BY item_type", (session_id,))}
                    else:
                        counts: dict[str, int] = {}
                        for row in connection.execute("SELECT item_json FROM thread_items WHERE thread_id=?", (session_id,)):
                            try:
                                kind = json.loads(row[0]).get("type", "")
                                counts[kind] = counts.get(kind, 0) + 1
                            except (json.JSONDecodeError, AttributeError, TypeError):
                                continue
                    native_user = sum(m.role == "user" and m.kind == "message" for m in existing.messages)
                    native_agent = sum(m.role == "assistant" and m.kind == "message" for m in existing.messages)
                    if counts.get("userMessage", 0) <= native_user and counts.get("agentMessage", 0) <= native_agent:
                        continue
                rows = connection.execute("SELECT item_json,created_at_ms FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal", (session_id,))
                messages: list[Message] = []
                records = list(existing.records if existing else data.get("_rollout_records", []))
                first_timestamp = ""
                last_timestamp = ""
                for row in rows:
                    try:
                        item = json.loads(row["item_json"])
                    except (json.JSONDecodeError, TypeError):
                        bundle.warnings.append(f"Ignored malformed history item in session {session_id}")
                        continue
                    if not isinstance(item, dict):
                        continue
                    timestamp = _iso(row["created_at_ms"], milliseconds=True)
                    first_timestamp = first_timestamp or timestamp
                    last_timestamp = timestamp
                    records.append({"type": "codex_history_item", "timestamp": timestamp, "payload": item})
                    messages.extend(_ui_messages(item, timestamp))
                if not messages:
                    continue
                if existing:
                    native_context = [message for message in existing.messages if message.role in {"system", "developer"} or message.kind == "summary"]
                    existing.messages = sorted(native_context + messages, key=lambda message: message.timestamp)
                    existing.records = records
                    existing.metadata["history_source"] = "sqlite_projection_with_native_context"
                    bundle.warnings.append(f"Session {session_id} had a partial native rollout; SQLite visible history was used with native instructions and summaries retained")
                    continue
                session = Session(id=session_id, title=session_id, cwd=str(data.get("cwd", "")), messages=messages, created_at=first_timestamp, updated_at=last_timestamp, archived=bool(data.get("archived", False)), metadata={"history_source": "sqlite_projection"}, records=records)
                self._enrich(session, data, names)
                if session.title == session_id:
                    session.title = next((m.content[:120].replace("\n", " ") for m in messages if m.role == "user"), session_id)
                bundle.sessions.append(session)
                seen.add(session_id)
                bundle.warnings.append(f"Session {session_id} imported from SQLite's visible-history projection; original native rollout was unavailable")
        except sqlite3.Error as error:
            bundle.warnings.append(f"Cannot read Codex SQLite conversation history: {error}")
        finally:
            connection.close()

    def _documents(self, home: Path, options: SourceOptions, bundle: Bundle) -> list[tuple[Path, Path]]:
        added: set[tuple[str, str, str]] = set()
        added_project_paths: set[Path] = set()
        project_configs: dict[Path, Path] = {}

        def add(path: Path, boundary: Path, relative: str, kind: str, scope: str = "global", project: str = "") -> None:
            identity = (scope, project, relative)
            if identity in added or not path.is_file():
                return
            if not _contained(path, boundary):
                bundle.warnings.append(f"Skipped document outside source root: {path}")
                return
            if scope == "project" and path.resolve() in added_project_paths:
                return
            if scope == "project" and kind == "instruction" and path.name == "AGENTS.md":
                override = path.with_name("AGENTS.override.md")
                if override.is_file() and _contained(override, boundary):
                    try:
                        if override.read_text(encoding="utf-8").strip():
                            # Only the effective Codex instruction participates
                            # in the portable bundle. Keeping an ignored base
                            # would feed our in-place target back into source.
                            # The writer backs up a retired native base before
                            # replacing it, and mapped source files stay intact.
                            return
                    except (OSError, UnicodeError):
                        pass
            try:
                raw = path.read_bytes()
                try:
                    content = raw.decode("utf-8")
                    encoding = "utf-8"
                except UnicodeDecodeError:
                    content = base64.b64encode(raw).decode("ascii")
                    encoding = "base64"
                if scope == "project" and kind == "instruction" and path.name == "AGENTS.md" and encoding == "utf-8" and "<!-- migrate-agent:codex:start -->" in content:
                    content = _without_own_project_section(content, path, bundle.warnings)
                if scope == "project" and kind == "instruction" and encoding == "utf-8":
                    # Native loaders trim rule boundaries. Canonicalize the
                    # portable form before the first write as well as rereads,
                    # so adding our managed block cannot change its identity.
                    content = content.rstrip() + "\n" if content.strip() else ""
                    if not content:
                        return
                executable = kind == "skill" and bool(path.stat().st_mode & 0o111)
                bundle.documents.append(Document(path=relative, content=content, kind=kind, scope=scope, project=project, encoding=encoding, executable=executable))
                added.add(identity)
                if scope == "project":
                    added_project_paths.add(path.resolve())
            except OSError as error:
                bundle.warnings.append(f"Cannot read document {path}: {error}")

        for name in ("AGENTS.md", "AGENTS.override.md", "instructions.md"):
            add(home / name, home, name, "instruction")
        for root_name in ("memories", "memory"):
            for path in _files(home / root_name, home, bundle.warnings):
                if path.suffix.lower() == ".md":
                    add(path, home, path.relative_to(home).as_posix(), "memory")
        if options.include_skills:
            builtin = home / "skills" / ".system"
            if builtin.exists():
                bundle.warnings.append("Codex built-in .system skills are excluded; only portable user skills are migrated")
            for path in _files(home / "skills", home, bundle.warnings):
                add(path, home, path.relative_to(home).as_posix(), "skill")
            # Codex also discovers user skills in the standard shared agents
            # directory. Do not infer arbitrary sibling locations for custom
            # --source-home directories.
            if home.name == ".codex":
                shared = home.parent / ".agents"
                if shared.is_symlink():
                    bundle.warnings.append(f"Skipped symbolic-link shared agents directory: {shared}")
                else:
                    for path in _files(shared / "skills", shared, bundle.warnings):
                        add(path, shared, path.relative_to(shared).as_posix(), "skill", "shared")
        if not options.include_project_docs:
            return []
        for selected in options.projects:
            project = Path(selected).expanduser().resolve()
            if not project.is_dir():
                bundle.warnings.append(f"Project root does not exist: {project}")
                continue
            for path in _files(project, project, bundle.warnings):
                if _contained(path, home):
                    # A custom CODEX_HOME may live inside the selected repo.
                    # Its globals/stores are handled separately, never as a
                    # nested project instruction to write into source home.
                    continue
                relative = path.relative_to(project)
                parts = relative.parts
                # Target Harness files are generated outputs, not a new Codex
                # source. In particular, a skill resource named AGENTS.md must
                # not become an unrelated project rule on repeated migration.
                if ".dsh" in parts:
                    continue
                if path.name == "config.toml" and path.parent.name == ".codex":
                    project_configs.setdefault(path, project)
                if options.include_skills and len(parts) >= 3 and parts[:2] in {(".agents", "skills"), (".codex", "skills")}:
                    add(path, project, relative.as_posix(), "skill", "project", str(project))
                elif path.name in {"AGENTS.md", "AGENTS.override.md"}:
                    add(path, project, relative.as_posix(), "instruction", "project", str(project))
                elif ".codex" in parts and path.suffix.lower() == ".md":
                    if "skills" not in parts or options.include_skills:
                        add(path, project, relative.as_posix(), "memory" if any(p in {"memory", "memories"} for p in parts) else "context", "project", str(project))
        return sorted(project_configs.items(), key=lambda entry: str(entry[0]))

    def _settings(self, home: Path, options: SourceOptions, bundle: Bundle, project_configs: list[tuple[Path, Path]]) -> None:
        configs: list[tuple[Path, str, str, list[Path]]] = []
        home_config = home / "config.toml"
        if home_config.is_file() and _contained(home_config, home):
            configs.append((home_config, "global", "", [home, *[Path(project).expanduser().resolve() for project in options.projects]]))
        for path, boundary in project_configs:
            configs.append((path, "project", str(path.parent.parent.resolve()), [boundary]))
        if not configs:
            return
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                bundle.warnings.append("Codex config.toml files were not imported: Python <3.11 requires optional tomli (install migrate-agent[codex])")
                return
        for path, scope, project, boundaries in configs:
            try:
                with path.open("rb") as stream:
                    config = tomllib.load(stream)
                # Only the home config contributes neutral settings. Project
                # configs contribute user instructions within their own scope.
                if scope == "global":
                    bundle.settings = {key: config[key] for key in ("model", "model_provider", "model_reasoning_effort", "personality", "project_doc_max_bytes", "project_doc_fallback_filenames") if key in config}
                developer_name = "config/developer_instructions.md" if scope == "global" else "codex-config-developer_instructions.md"
                model_name = "config/model_instructions.md" if scope == "global" else "codex-config-model_instructions.md"
                developer = config.get("developer_instructions")
                if isinstance(developer, str) and developer:
                    bundle.documents.append(Document(path=developer_name, content=developer, kind="instruction", scope=scope, project=project))
                custom_file = config.get("model_instructions_file", config.get("experimental_instructions_file"))
                if isinstance(custom_file, str) and custom_file:
                    custom_path = Path(custom_file).expanduser()
                    if not custom_path.is_absolute():
                        custom_path = path.parent / custom_path
                    if not any(_contained(custom_path, boundary) for boundary in boundaries):
                        bundle.warnings.append(f"Codex model_instructions_file in {path} points outside selected source roots; select its project root explicitly to include those user instructions")
                    elif custom_path.is_file():
                        try:
                            bundle.documents.append(Document(path=model_name, content=custom_path.read_text(encoding="utf-8"), kind="instruction", scope=scope, project=project))
                        except (OSError, UnicodeError):
                            bundle.warnings.append(f"Codex model_instructions_file in {path} could not be read as UTF-8 user instructions")
                    else:
                        bundle.warnings.append(f"Codex model_instructions_file in {path} does not exist; custom model instructions were not migrated")
                if any(key in config for key in ("mcp_servers", "hooks", "plugins", "model_providers")):
                    bundle.warnings.append("Codex MCP servers, hooks, plugins and provider credentials require target-specific setup; no executable configuration or credentials were copied")
            except (OSError, ValueError) as error:
                bundle.warnings.append(f"Cannot parse Codex configuration {path}: {type(error).__name__}")
