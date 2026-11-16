"""Reviewable plans, transactional writes, and checksum-guarded rollback.

Adapters describe files; only this module mutates the destination. A plan never
creates a directory or reads file contents into its public JSON representation.
"""

import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional

from .models import Bundle, PlannedFile, TargetOptions, TargetPlan


class MigrationError(ValueError):
    """A migration cannot safely proceed."""


class ConflictError(MigrationError):
    pass


class ConcurrentMigrationError(MigrationError):
    pass


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _absolute(path: Path) -> Path:
    # Do not resolve: following a symlink would hide it from subsequent checks.
    absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
    if sys.platform == "darwin":
        # macOS supplies these immutable OS aliases, including TMPDIR=/var/...
        # Normalize only the exact OS aliases; user-created symlinks remain visible.
        for alias in ("/tmp", "/var", "/etc"):
            alias_path = Path(alias)
            expected = Path("/private" + alias)
            alias_target = alias_path.parent / alias_path.readlink() if alias_path.is_symlink() else None
            if alias_target == expected and _within(absolute, alias_path):
                absolute = expected / absolute.relative_to(alias_path)
                break
    return absolute


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_chain(path: Path) -> None:
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise MigrationError("Refusing symlink in destination: %s" % part)
        if part != path and part.exists() and not part.is_dir():
            raise MigrationError("Destination parent is not a directory: %s" % part)


def _relative_path(value: str) -> Path:
    # Reject Windows paths on Unix too: interchange plans may be cross-platform.
    win = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (not value or "\\" in value or "\x00" in value or posix.is_absolute()
            or win.drive or any(p in ("", ".", "..") for p in value.split("/"))):
        raise MigrationError("Unsafe target-relative path: %r" % value)
    return Path(*posix.parts)


def _digest(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    if not path.is_file():
        raise MigrationError("Destination is not a regular file: %s" % path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_mode(path: Path) -> Optional[int]:
    return stat.S_IMODE(path.stat().st_mode) if path.exists() else None


def _validate_output_mode(mode: int) -> None:
    if mode not in (0o600, 0o700):
        raise MigrationError("Planned file mode must be owner-only 0600 or 0700")


def _atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    _safe_chain(path)
    fd, temporary = tempfile.mkstemp(prefix=".migrate-agent-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _archive_bytes(bundle: Bundle) -> bytes:
    # The archive can contain an entire history store. Avoid constructing a
    # second giant Unicode JSON string before allocating its UTF-8 bytes.
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    output = io.BytesIO()
    for chunk in encoder.iterencode(bundle.to_dict()):
        output.write(chunk.encode("utf-8"))
    output.write(b"\n")
    return output.getvalue()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mapped_project(project: str, mappings: Dict[str, str]) -> Path:
    """Resolve the same longest-prefix project mapping used by adapters."""
    source = _absolute(Path(project)).resolve()
    candidates = sorted(mappings.items(), key=lambda pair: len(Path(pair[0]).parts), reverse=True)
    for before, after in candidates:
        try:
            relative = source.relative_to(_absolute(Path(before)).resolve())
        except ValueError:
            continue
        return _absolute(Path(after) / relative)
    return _absolute(Path(project))


def _project_root_name(name: str) -> bool:
    token = name[len("project:"):] if isinstance(name, str) and name.startswith("project:") else ""
    return bool(token) and len(token) <= 128 and all(character.isascii() and (character.isalnum() or character in "_-") for character in token)


def _validate_project_path(relative: Path) -> None:
    # Project conversion may add native files, but never modify the source
    # client's own configuration or repository metadata through another root.
    if any(part in {".git", ".codex", ".migrate-agent"} for part in relative.parts):
        raise MigrationError("Refusing protected project destination: %s" % relative)


@dataclass
class PlanEntry:
    path: str
    root: str
    destination: Path
    action: str
    sha256: str
    previous_sha256: Optional[str]
    content: bytes = field(repr=False)
    description: str = ""
    mode: int = 0o600
    previous_mode: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path, "root": self.root, "destination": str(self.destination),
            "action": self.action, "sha256": self.sha256,
            "previous_sha256": self.previous_sha256, "size": len(self.content),
            "description": self.description, "mode": self.mode,
            "previous_mode": self.previous_mode,
        }


@dataclass
class MigrationPlan:
    source: str
    roots: Dict[str, Path]
    entries: List[PlanEntry]
    conflict_policy: str
    sessions: int
    documents: int
    warnings: List[str]
    session_map: Dict[str, str]
    instructions: List[str]

    def to_dict(self) -> Dict[str, Any]:
        counts = {key: sum(e.action == key for e in self.entries)
                  for key in ("create", "update", "unchanged", "skip", "conflict")}
        return {
            "source": self.source, "target_home": str(self.roots["home"]),
            "project_roots": {key: str(value) for key, value in self.roots.items() if key.startswith("project:")},
            "conflict_policy": self.conflict_policy,
            "files": [e.to_dict() for e in self.entries],
            "summary": dict(sessions=self.sessions, documents=self.documents,
                            projects=len({e.root for e in self.entries if e.root.startswith("project:")}), **counts),
            "warnings": list(self.warnings), "session_map": dict(self.session_map),
            "instructions": list(self.instructions),
        }


class MigrationEngine:
    def prepare(self, bundle: Bundle, target: TargetPlan, options: TargetOptions,
                conflict: str = "abort") -> MigrationPlan:
        if conflict not in ("abort", "overwrite", "skip"):
            raise MigrationError("Conflict policy must be abort, overwrite, or skip")
        roots = {"home": _absolute(options.home), "agents": _absolute(options.agent_root)}
        allowed_projects = {_mapped_project(document.project, options.project_map).resolve()
                            for document in bundle.documents
                            if document.scope == "project" and document.project}
        for name, destination in target.project_roots.items():
            if not _project_root_name(name):
                raise MigrationError("Invalid project destination root id: %s" % name)
            root = _absolute(Path(destination))
            if root.resolve() not in allowed_projects:
                raise MigrationError("Project destination is not a mapped source project: %s" % root)
            roots[name] = root
        used_roots = {"home", *(file.root for file in target.files)}
        unknown_roots = used_roots - roots.keys()
        if unknown_roots:
            raise MigrationError("Unknown destination root: %s" % sorted(unknown_roots)[0])
        source_root = _absolute(Path(bundle.source_root)) if bundle.source_root else None
        for name in used_roots:
            root = roots[name]
            _safe_chain(root)
            if root.exists() and not root.is_dir():
                raise MigrationError("Target root is not a directory: %s" % root)
            if source_root is not None:
                resolved_source, resolved_target = source_root.resolve(), root.resolve()
                if _within(resolved_target, resolved_source) or (not name.startswith("project:") and _within(resolved_source, resolved_target)):
                    raise MigrationError("Source and destination roots overlap: %s" % root)
        archive = _archive_bytes(bundle)
        archive_path = ".migrate-agent/bundles/%s.json" % _sha(archive)
        files = [*target.files, PlannedFile(archive_path, archive, "Portable source archive")]
        entries = []
        seen = set()
        warnings = list(dict.fromkeys([*bundle.warnings, *target.warnings]))
        for file in files:
            if file.root not in roots:
                raise MigrationError("Unknown destination root: %s" % file.root)
            relative = _relative_path(file.path)
            if file.root.startswith("project:"):
                _validate_project_path(relative)
            if relative.parts[0] == ".migrate-agent" and file.path != archive_path:
                raise MigrationError("Adapter cannot write migration control files")
            destination = roots[file.root] / relative
            _safe_chain(destination)
            if source_root is not None and _within(destination, source_root):
                raise MigrationError("Destination would modify source data: %s" % destination)
            if str(destination) in seen:
                raise MigrationError("Duplicate destination in migration: %s" % destination)
            seen.add(str(destination))
            if not isinstance(file.content, bytes):
                raise MigrationError("Planned file content must be bytes: %s" % file.path)
            _validate_output_mode(file.mode)
            before = _digest(destination)
            before_mode = _file_mode(destination)
            after = _sha(file.content)
            executable_changed = before_mode is not None and bool(before_mode & stat.S_IXUSR) != bool(file.mode & stat.S_IXUSR)
            if before == after and not executable_changed:
                action = "unchanged"
            elif before is None:
                action = "create"
            elif conflict == "overwrite":
                action = "update"
            elif conflict == "skip":
                action = "skip"
                warnings.append("Kept existing file due to conflict: %s" % destination)
            elif before == after and executable_changed:
                # Repair executable capability without requiring a content
                # overwrite policy. The original mode is backed up as usual.
                action = "update"
            elif file.merge:
                # The adapter computed a managed merge preserving existing
                # content. The usual backup and change-since-plan checks apply.
                action = "update"
            else:
                action = "conflict"
            entries.append(PlanEntry(file.path, file.root, destination, action,
                                     after, before, file.content, file.description,
                                     mode=file.mode, previous_mode=before_mode))
        return MigrationPlan(bundle.source, roots, entries, conflict, len(bundle.sessions),
                             len(bundle.documents), warnings, dict(target.session_map),
                             list(target.instructions))

    @staticmethod
    def _validate_entry(entry: PlanEntry, roots: Dict[str, Path]) -> None:
        if entry.root not in roots or roots[entry.root] / _relative_path(entry.path) != entry.destination:
            raise MigrationError("Plan destination no longer matches its root")
        _safe_chain(entry.destination)
        if entry.root.startswith("project:"):
            _validate_project_path(_relative_path(entry.path))
        if _sha(entry.content) != entry.sha256:
            raise MigrationError("Plan content changed after preparation: %s" % entry.destination)
        _validate_output_mode(entry.mode)
        if _digest(entry.destination) != entry.previous_sha256:
            raise ConflictError("Destination changed after preview: %s" % entry.destination)
        if _file_mode(entry.destination) != entry.previous_mode:
            raise ConflictError("Destination permissions changed after preview: %s" % entry.destination)

    @staticmethod
    def _mkdir(path: Path, created: Optional[List[str]] = None) -> None:
        _safe_chain(path)
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                # Disjoint concurrent writes can need the same new parent.
                _safe_chain(directory)
                if not directory.is_dir():
                    raise MigrationError("Destination parent is not a directory: %s" % directory)
                continue
            if created is not None:
                created.append(str(directory))

    @contextmanager
    def _destination_locks(self, destinations: List[Path]):
        # Locks are outside projects so rollback does not leave project control
        # files behind. Exact destination identity also covers overlapping
        # parent/child project roots used with different Harness homes.
        identity = _sha(str(Path.home().resolve()).encode("utf-8"))[:16]
        lock_home = _absolute(Path(tempfile.gettempdir())) / ("migrate-agent-project-locks-" + identity)
        self._mkdir(lock_home)
        with ExitStack() as stack:
            for destination in sorted(set(destinations), key=str):
                name = _sha(str(destination).encode("utf-8")) + ".lock"
                stack.enter_context(self._exclusive_lock(lock_home / name))
            yield

    @contextmanager
    def _exclusive_lock(self, lock_path: Path):
        _safe_chain(lock_path)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ConcurrentMigrationError(
                "Migration lock exists: %s. If no migration is running, remove this stale lock explicitly."
                % lock_path
            )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "created_at": _now()}, handle)
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def _lock(self, home: Path):
        control = home / ".migrate-agent"
        self._mkdir(control)
        lock_path = control / "migration.lock"
        with self._exclusive_lock(lock_path):
            yield

    @contextmanager
    def _operation_locks(self, home: Path, destinations: List[Path], has_projects: bool):
        with ExitStack() as stack:
            stack.enter_context(self._lock(home))
            if has_projects:
                stack.enter_context(self._destination_locks(destinations))
            yield

    def apply(self, plan: MigrationPlan) -> Dict[str, Any]:
        if any(entry.action == "conflict" for entry in plan.entries):
            conflicts = sum(entry.action == "conflict" for entry in plan.entries)
            raise ConflictError("%s existing files conflict; review preview and choose overwrite or skip" % conflicts)
        for entry in plan.entries:
            self._validate_entry(entry, plan.roots)
        changes = [entry for entry in plan.entries if entry.action in ("create", "update")]
        result = plan.to_dict()
        if not changes:
            result.update(status="unchanged", manifest_path=None, run_id=None)
            return result
        has_projects = any(entry.root.startswith("project:") for entry in changes)
        with self._operation_locks(plan.roots["home"], [entry.destination for entry in changes], has_projects):
            for entry in plan.entries:
                self._validate_entry(entry, plan.roots)
            run_id = str(uuid.uuid4())
            run_dir = plan.roots["home"] / ".migrate-agent" / "runs" / run_id
            self._mkdir(run_dir / "backups")
            manifest_path = run_dir / "manifest.json"
            journal = {
                "schema_version": 1, "run_id": run_id, "source": plan.source,
                "created_at": _now(), "status": "applying",
                "roots": {key: str(value) for key, value in plan.roots.items()},
                "files": [], "created_directories": [],
            }
            for index, entry in enumerate(changes):
                record = entry.to_dict()
                record["state"] = "pending"
                record["backup"] = None
                if entry.previous_sha256 is not None:
                    record["backup"] = "backups/%06d" % index
                    original = entry.destination.read_bytes()
                    if _sha(original) != entry.previous_sha256 or _file_mode(entry.destination) != entry.previous_mode:
                        raise ConflictError("Destination changed while preparing backup: %s" % entry.destination)
                    _atomic_write(run_dir / record["backup"], original)
                journal["files"].append(record)
            _atomic_write(manifest_path, _json_bytes(journal))
            try:
                for entry, record in zip(changes, journal["files"]):
                    self._validate_entry(entry, plan.roots)
                    self._mkdir(entry.destination.parent, journal["created_directories"])
                    # Persist intent before replace; recovery also handles a crash immediately after replace.
                    record["state"] = "writing"
                    _atomic_write(manifest_path, _json_bytes(journal))
                    _atomic_write(entry.destination, entry.content, mode=entry.mode)
                    record["state"] = "applied"
                    _atomic_write(manifest_path, _json_bytes(journal))
                journal["status"] = "completed"
                journal["completed_at"] = _now()
                _atomic_write(manifest_path, _json_bytes(journal))
            except BaseException:
                # Keep a durable, recoverable journal even for Ctrl-C.
                journal["status"] = "failed"
                journal["failed_at"] = _now()
                try:
                    self._restore(journal, run_dir, failed_apply=True)
                    journal["status"] = "failed_rolled_back"
                except (OSError, MigrationError) as rollback_error:
                    journal["recovery_error"] = str(rollback_error)
                _atomic_write(manifest_path, _json_bytes(journal))
                raise
        result.update(status="completed", manifest_path=str(manifest_path), run_id=run_id)
        return result

    def _restore(self, journal: Dict[str, Any], run_dir: Path,
                 failed_apply: bool = False) -> Dict[str, int]:
        roots = {key: _absolute(Path(value)) for key, value in journal["roots"].items()}
        work = []
        for record in journal["files"]:
            if record.get("state") not in ("applied", "writing"):
                continue
            root = roots.get(record["root"])
            if root is None:
                raise MigrationError("Unknown manifest destination root")
            destination = root / _relative_path(record["path"])
            if record["root"].startswith("project:"):
                if not _project_root_name(record["root"]):
                    raise MigrationError("Invalid project root in manifest")
                _validate_project_path(_relative_path(record["path"]))
            if str(destination) != record["destination"]:
                raise MigrationError("Manifest destination is inconsistent")
            _safe_chain(destination)
            current = _digest(destination)
            current_mode = _file_mode(destination)
            previous_mode = record.get("previous_mode")
            mode = record.get("mode", 0o600)
            _validate_output_mode(mode)
            if current == record["previous_sha256"] and current_mode == previous_mode:
                # An intent was recorded, but the replace did not happen (or recovery already restored it).
                continue
            if current != record["sha256"] or current_mode != mode:
                raise ConflictError("Refusing rollback of a modified file: %s" % destination)
            backup = None
            if record["previous_sha256"] is not None:
                if not record.get("backup"):
                    raise MigrationError("Missing rollback backup")
                backup_path = run_dir / _relative_path(record["backup"])
                if backup_path.parent != run_dir / "backups":
                    raise MigrationError("Invalid backup location")
                _safe_chain(backup_path)
                if _digest(backup_path) != record["previous_sha256"]:
                    raise MigrationError("Rollback backup checksum mismatch: %s" % backup_path)
                backup = backup_path
            work.append((record, destination, backup))
        counts = {"restored": 0, "deleted": 0}
        for record, destination, backup in reversed(work):
            # Recheck after preflight so edits made while backups are read are preserved.
            _safe_chain(destination)
            if _digest(destination) != record["sha256"] or _file_mode(destination) != record.get("mode", 0o600):
                raise ConflictError("Destination changed during rollback: %s" % destination)
            if backup is None:
                destination.unlink()
                counts["deleted"] += 1
            else:
                # Keep only one backup's bytes in memory, even for a rollback
                # that restores thousands of imported native sessions.
                _safe_chain(backup)
                original = backup.read_bytes()
                if _sha(original) != record["previous_sha256"]:
                    raise MigrationError("Rollback backup changed after preflight: %s" % backup)
                previous_mode = record.get("previous_mode")
                _atomic_write(destination, original, previous_mode if previous_mode is not None else 0o600)
                counts["restored"] += 1
            record["state"] = "restored"
        for directory in reversed(journal.get("created_directories", [])):
            path = _absolute(Path(directory))
            if any(_within(path, root) for root in roots.values()):
                _safe_chain(path)
                try:
                    path.rmdir()
                except (FileNotFoundError, OSError):
                    # A nonempty directory may contain user-created files.
                    pass
        return counts

    def rollback(self, manifest_path: Path) -> Dict[str, Any]:
        manifest_path = _absolute(manifest_path)
        _safe_chain(manifest_path)
        try:
            journal = json.loads(manifest_path.read_text(encoding="utf-8"))
            if journal.get("schema_version") != 1:
                raise MigrationError("Unsupported migration manifest version")
            home = _absolute(Path(journal["roots"]["home"]))
            run_id = journal["run_id"]
            if str(uuid.UUID(run_id)) != run_id:
                raise MigrationError("Invalid migration run id")
            expected = home / ".migrate-agent" / "runs" / run_id / "manifest.json"
            if manifest_path != expected:
                raise MigrationError("Manifest is outside its migration journal directory")
            if journal["status"] in ("rolled_back", "failed_rolled_back"):
                return {"status": "rolled_back", "restored": 0, "deleted": 0,
                        "manifest_path": str(manifest_path)}
            if journal["status"] not in ("completed", "failed", "applying"):
                raise MigrationError("Unsupported migration journal state")
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise MigrationError("Invalid migration manifest: %s" % error) from error
        has_projects = any(record.get("root", "").startswith("project:") for record in journal["files"])
        with self._operation_locks(home, [Path(record["destination"]) for record in journal["files"]], has_projects):
            counts = self._restore(journal, manifest_path.parent)
            journal["status"] = "rolled_back"
            journal["rolled_back_at"] = _now()
            _atomic_write(manifest_path, _json_bytes(journal))
        return dict(status="rolled_back", manifest_path=str(manifest_path), **counts)
