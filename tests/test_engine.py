import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from migrate_agent.engine import (
    ConcurrentMigrationError, ConflictError, MigrationEngine, MigrationError,
)
from migrate_agent.models import Bundle, Document, Message, PlannedFile, Session, TargetOptions, TargetPlan


class MigrationEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / ".dsh"
        self.agents = self.root / ".agents"
        self.engine = MigrationEngine()
        self.options = TargetOptions(self.home, agents_home=self.agents)
        self.bundle = Bundle("codex", sessions=[Session("a", "Title", "/project", [Message("user", "Secret transcript")])])

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, files=None, conflict="abort"):
        return self.engine.prepare(
            self.bundle,
            TargetPlan(files or [PlannedFile("sessions/a.jsonl", b"native transcript")]),
            self.options, conflict,
        )

    def test_preview_does_not_write_or_disclose_contents(self):
        plan = self.prepare()
        self.assertFalse(self.home.exists())
        public = json.dumps(plan.to_dict())
        self.assertNotIn("Secret transcript", public)
        self.assertNotIn("native transcript", public)
        self.assertEqual(plan.to_dict()["summary"]["create"], 2)

    def test_system_temporary_aliases_are_normalized(self):
        from migrate_agent.engine import _absolute
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "target"
            options = TargetOptions(path, agents_home=Path(temporary) / "agents")
            plan = self.engine.prepare(self.bundle, TargetPlan([PlannedFile("test", b"test")]), options)
            self.assertEqual(plan.roots["home"], path.resolve())
        if Path("/tmp").is_symlink() and os.uname().sysname == "Darwin":
            self.assertEqual(_absolute(Path("/tmp/example")), Path("/private/tmp/example"))

    def test_can_recover_intent_journal_after_process_crash(self):
        result = self.engine.apply(self.prepare())
        manifest = Path(result["manifest_path"])
        journal = json.loads(manifest.read_text())
        journal["status"] = "applying"
        for record in journal["files"]:
            record["state"] = "writing"
        manifest.write_text(json.dumps(journal))
        rolled = self.engine.rollback(manifest)
        self.assertEqual(rolled["deleted"], 2)
        self.assertFalse((self.home / "sessions/a.jsonl").exists())

    def test_apply_roundtrip_and_idempotent_repeat(self):
        applied = self.engine.apply(self.prepare())
        self.assertEqual(applied["status"], "completed")
        native = self.home / "sessions/a.jsonl"
        self.assertEqual(native.read_bytes(), b"native transcript")
        self.assertEqual(native.stat().st_mode & 0o777, 0o600)
        archives = list((self.home / ".migrate-agent/bundles").glob("*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads(archives[0].read_text())["sessions"][0]["messages"][0]["content"], "Secret transcript")
        repeated = self.engine.apply(self.prepare())
        self.assertEqual(repeated["status"], "unchanged")
        self.assertIsNone(repeated["manifest_path"])
        rolled = self.engine.rollback(Path(applied["manifest_path"]))
        self.assertEqual(rolled["deleted"], 2)
        self.assertFalse(native.exists())
        self.assertFalse(native.parent.exists())
        self.assertEqual(self.engine.rollback(Path(applied["manifest_path"]))["deleted"], 0)

    def test_conflict_abort_preflight_prevents_other_writes(self):
        destination = self.home / "sessions/a.jsonl"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing")
        plan = self.prepare()
        self.assertEqual(plan.to_dict()["summary"]["conflict"], 1)
        with self.assertRaises(ConflictError):
            self.engine.apply(plan)
        self.assertFalse((self.home / ".migrate-agent").exists())
        self.assertEqual(destination.read_bytes(), b"existing")

    def test_overwrite_backups_and_restores_mode(self):
        destination = self.agents / "AGENTS.md"
        destination.parent.mkdir()
        destination.write_bytes(b"my instructions")
        destination.chmod(0o640)
        result = self.engine.apply(self.prepare([PlannedFile("AGENTS.md", b"merged", root="agents")], "overwrite"))
        self.assertEqual(destination.read_bytes(), b"merged")
        rolled = self.engine.rollback(Path(result["manifest_path"]))
        self.assertEqual(rolled["restored"], 1)
        self.assertEqual(destination.read_bytes(), b"my instructions")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o640)

    def test_skip_preserves_existing_and_reports(self):
        destination = self.home / "sessions/a.jsonl"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing")
        plan = self.prepare(conflict="skip")
        self.assertEqual(plan.to_dict()["summary"]["skip"], 1)
        self.assertTrue(plan.warnings)
        self.engine.apply(plan)
        self.assertEqual(destination.read_bytes(), b"existing")

    def test_change_since_preview_aborts_without_journal(self):
        plan = self.prepare()
        destination = self.home / "sessions/a.jsonl"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"new work")
        with self.assertRaises(ConflictError):
            self.engine.apply(plan)
        self.assertFalse((self.home / ".migrate-agent").exists())
        self.assertEqual(destination.read_bytes(), b"new work")

    def test_rollback_refuses_user_edits_before_touching_any_file(self):
        result = self.engine.apply(self.prepare([
            PlannedFile("one", b"one"), PlannedFile("two", b"two")
        ]))
        (self.home / "one").write_bytes(b"user work")
        with self.assertRaises(ConflictError):
            self.engine.rollback(Path(result["manifest_path"]))
        self.assertEqual((self.home / "one").read_bytes(), b"user work")
        self.assertEqual((self.home / "two").read_bytes(), b"two")

    def test_unsafe_paths_and_duplicate_outputs_rejected(self):
        for path in ["../escape", "/absolute", "a/../b", "a//b", "C:/windows", "a\\b", ".", ".migrate-agent/migration.lock"]:
            with self.subTest(path=path), self.assertRaises(MigrationError):
                self.prepare([PlannedFile(path, b"test")])
        with self.assertRaises(MigrationError):
            self.prepare([PlannedFile("same", b"one"), PlannedFile("same", b"two")])
        self.assertFalse(self.home.exists())

    def test_symlink_output_and_source_overlap_rejected(self):
        self.home.mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.home / "sessions").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(MigrationError):
            self.prepare()
        self.bundle.source_root = str(self.root)
        with self.assertRaises(MigrationError):
            self.prepare([PlannedFile("safe", b"test")])

    def test_new_symlink_since_preview_rejected(self):
        plan = self.prepare()
        self.home.mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.home / "sessions").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(MigrationError):
            self.engine.apply(plan)
        self.assertFalse(list(elsewhere.iterdir()))

    def test_unused_symlink_agents_root_does_not_block_home_outputs(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.agents.symlink_to(elsewhere, target_is_directory=True)
        result = self.engine.apply(self.prepare())
        self.assertEqual(result["status"], "completed")
        self.assertFalse(list(elsewhere.iterdir()))
        with self.assertRaises(MigrationError):
            self.prepare([PlannedFile("AGENTS.md", b"instructions", root="agents")])
        self.engine.rollback(Path(result["manifest_path"]))

    def test_unused_agents_root_may_overlap_source(self):
        source = self.root / ".codex"
        source.mkdir()
        self.bundle.source_root = str(source)
        self.options = TargetOptions(self.home, agents_home=source)
        plan = self.prepare()
        self.assertEqual(plan.to_dict()["summary"]["create"], 2)
        with self.assertRaises(MigrationError):
            self.prepare([PlannedFile("AGENTS.md", b"instructions", root="agents")])

    def test_concurrent_lock_refuses_without_mutating_lock(self):
        control = self.home / ".migrate-agent"
        control.mkdir(parents=True)
        lock = control / "migration.lock"
        lock.write_text("existing migration")
        with self.assertRaises(ConcurrentMigrationError):
            self.engine.apply(self.prepare())
        self.assertEqual(lock.read_text(), "existing migration")
        self.assertFalse((self.home / "sessions").exists())

    def test_partial_failure_restores_original_and_leaves_journal(self):
        destination = self.home / "one"
        destination.parent.mkdir()
        destination.write_bytes(b"original")
        plan = self.prepare([PlannedFile("one", b"updated"), PlannedFile("two", b"new")], "overwrite")
        from migrate_agent import engine
        actual_write = engine._atomic_write

        def fail_second(path, content, mode=0o600):
            if path == self.home / "two":
                raise OSError("simulated disk error")
            return actual_write(path, content, mode)

        with mock.patch.object(engine, "_atomic_write", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.engine.apply(plan)
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertFalse((self.home / "two").exists())
        manifests = list((self.home / ".migrate-agent/runs").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["status"], "failed_rolled_back")
        self.assertFalse((self.home / ".migrate-agent/migration.lock").exists())

    def test_backup_corruption_refuses_all_rollback(self):
        destination = self.home / "one"
        destination.parent.mkdir()
        destination.write_bytes(b"original")
        result = self.engine.apply(self.prepare([PlannedFile("one", b"updated")], "overwrite"))
        manifest = Path(result["manifest_path"])
        backup = next((manifest.parent / "backups").iterdir())
        backup.write_bytes(b"corrupt")
        with self.assertRaises(MigrationError):
            self.engine.rollback(manifest)
        self.assertEqual(destination.read_bytes(), b"updated")

    def project_plan(self, project, files=None, mappings=None, conflict="abort"):
        self.bundle.documents = [Document("AGENTS.md", "project rules", scope="project", project=str(project))]
        root = "project:example"
        destination = project
        if mappings:
            from migrate_agent.engine import _mapped_project
            destination = _mapped_project(str(project), mappings)
        target = TargetPlan(
            files=files if files is not None else [
                PlannedFile("AGENTS.md", b"native project rules", root=root),
                PlannedFile(".dsh/skills/demo/SKILL.md", b"native skill", root=root),
                PlannedFile(".dsh/skills/demo/assets/data.bin", b"\x00\xff", root=root),
            ],
            project_roots={root: destination},
        )
        options = TargetOptions(self.home, agents_home=self.agents, project_map=mappings or {})
        return self.engine.prepare(self.bundle, target, options, conflict)

    def test_native_project_files_are_reviewable_and_rollback_together(self):
        project = self.root / "project"
        project.mkdir()
        (project / ".git").mkdir()
        plan = self.project_plan(project)
        public = plan.to_dict()
        self.assertEqual(public["summary"]["projects"], 1)
        self.assertEqual(public["project_roots"], {"project:example": str(project)})
        self.assertFalse((project / "AGENTS.md").exists())
        self.assertFalse(self.home.exists())
        result = self.engine.apply(plan)
        self.assertEqual((project / "AGENTS.md").read_bytes(), b"native project rules")
        self.assertEqual((project / ".dsh/skills/demo/assets/data.bin").read_bytes(), b"\x00\xff")
        journal = json.loads(Path(result["manifest_path"]).read_text())
        self.assertEqual(journal["roots"]["project:example"], str(project))
        repeated = self.engine.apply(self.project_plan(project))
        self.assertEqual(repeated["status"], "unchanged")
        rolled = self.engine.rollback(Path(result["manifest_path"]))
        self.assertEqual(rolled["deleted"], 4)
        self.assertTrue((project / ".git").is_dir())
        self.assertFalse((project / ".dsh").exists())
        self.assertFalse((project / ".migrate-agent").exists())

    def test_project_destination_uses_longest_component_aware_mapping(self):
        source = self.root / "old" / "sub" / "nested"
        target = self.root / "new-specific" / "nested"
        mappings = {str(self.root / "old"): str(self.root / "new"),
                    str(self.root / "old" / "sub"): str(self.root / "new-specific")}
        plan = self.project_plan(source, mappings=mappings)
        self.assertEqual(plan.roots["project:example"], target)
        result = self.engine.apply(plan)
        self.assertTrue((target / "AGENTS.md").exists())
        self.assertFalse(source.exists())
        self.engine.rollback(Path(result["manifest_path"]))
        self.assertFalse(target.exists())

    def test_managed_project_merge_defaults_to_backed_up_update(self):
        project = self.root / "project"
        project.mkdir()
        destination = project / "AGENTS.local.md"
        original = b"Target user's original rules\n"
        destination.write_bytes(original)
        destination.chmod(0o640)
        merged = original + b"\n<!-- managed -->\nImported rule\n"
        plan = self.project_plan(project, [PlannedFile("AGENTS.local.md", merged, root="project:example", merge=True)])
        self.assertEqual(plan.to_dict()["summary"]["update"], 1)
        result = self.engine.apply(plan)
        self.assertEqual(destination.read_bytes(), merged)
        rolled = self.engine.rollback(Path(result["manifest_path"]))
        self.assertEqual(rolled["restored"], 1)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o640)

    def test_managed_global_merge_and_explicit_skip(self):
        destination = self.home / "AGENTS.md"
        destination.parent.mkdir()
        destination.write_bytes(b"existing")
        plan = self.prepare([PlannedFile("AGENTS.md", b"existing\nmanaged", merge=True)])
        self.assertEqual(plan.to_dict()["summary"]["update"], 1)
        result = self.engine.apply(plan)
        self.engine.rollback(Path(result["manifest_path"]))
        skipped = self.prepare([PlannedFile("AGENTS.md", b"existing\nmanaged", merge=True)], "skip")
        self.assertEqual(skipped.to_dict()["summary"]["skip"], 1)
        self.engine.apply(skipped)
        self.assertEqual(destination.read_bytes(), b"existing")

    def test_managed_merge_still_refuses_changes_since_preview(self):
        project = self.root / "project"
        project.mkdir()
        destination = project / "AGENTS.md"
        destination.write_bytes(b"existing")
        plan = self.project_plan(project, [PlannedFile("AGENTS.md", b"existing\nmanaged", root="project:example", merge=True)])
        destination.write_bytes(b"user updated after preview")
        with self.assertRaises(ConflictError):
            self.engine.apply(plan)
        self.assertEqual(destination.read_bytes(), b"user updated after preview")
        self.assertFalse(self.home.exists())

    def test_project_roots_require_registered_mapped_source_projects(self):
        project = self.root / "project"
        self.bundle.documents = [Document("AGENTS.md", "rules", scope="project", project=str(project))]
        for name, root in [("home", project), ("project:", project), ("project:../bad", project),
                           ("project:example", self.root / "unselected")]:
            with self.subTest(name=name, root=root), self.assertRaises(MigrationError):
                self.engine.prepare(self.bundle, TargetPlan(project_roots={name: root}), self.options)
        with self.assertRaises(MigrationError):
            self.engine.prepare(self.bundle, TargetPlan([PlannedFile("AGENTS.md", b"rules", root="project:missing")]), self.options)
        self.assertFalse(self.home.exists())

    def test_project_paths_do_not_modify_source_client_or_git_metadata(self):
        project = self.root / "project"
        for relative in (".git/config", ".codex/config.toml", "nested/.git/HEAD", ".migrate-agent/manifest.json", "../escape"):
            with self.subTest(relative=relative), self.assertRaises(MigrationError):
                self.project_plan(project, [PlannedFile(relative, b"data", root="project:example")])
        source_home = project / ".codex"
        source_home.mkdir(parents=True)
        original = source_home / "AGENTS.md"
        original.write_bytes(b"source configuration")
        self.bundle.source_root = str(source_home)
        result = self.engine.apply(self.project_plan(project))
        self.assertEqual(original.read_bytes(), b"source configuration")
        self.engine.rollback(Path(result["manifest_path"]))
        with self.assertRaises(MigrationError):
            self.project_plan(source_home)

    def test_project_symlink_root_and_intermediate_directory_rejected(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(MigrationError):
            self.project_plan(link)
        project = self.root / "project"
        project.mkdir()
        (project / ".dsh").symlink_to(real, target_is_directory=True)
        with self.assertRaises(MigrationError):
            self.project_plan(project)
        self.assertFalse(list(real.iterdir()))

    def test_different_harness_homes_share_project_destination_locks(self):
        project = self.root / "project"
        plan = self.project_plan(project)
        destination = project / "AGENTS.md"
        with self.engine._destination_locks([destination]):
            other = MigrationEngine()
            with self.assertRaises(ConcurrentMigrationError):
                other.apply(plan)
        self.assertFalse(destination.exists())
        result = self.engine.apply(plan)
        self.engine.rollback(Path(result["manifest_path"]))

    def test_multi_root_failure_restores_project_changes_before_returning(self):
        project = self.root / "project"
        project.mkdir()
        original = project / "AGENTS.md"
        original.write_bytes(b"original rules")
        plan = self.project_plan(project, [
            PlannedFile("AGENTS.md", b"original rules\nmanaged", root="project:example", merge=True),
            PlannedFile("sessions/fail.jsonl", b"new session"),
        ])
        from migrate_agent import engine
        actual_write = engine._atomic_write

        def fail_session(path, content, mode=0o600):
            if path == self.home / "sessions/fail.jsonl":
                raise OSError("simulated second-root disk error")
            return actual_write(path, content, mode)

        with mock.patch.object(engine, "_atomic_write", side_effect=fail_session), self.assertRaises(OSError):
            self.engine.apply(plan)
        self.assertEqual(original.read_bytes(), b"original rules")
        self.assertFalse((self.home / "sessions/fail.jsonl").exists())
        manifest = next((self.home / ".migrate-agent/runs").glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text())["status"], "failed_rolled_back")

    def test_executable_skill_script_can_run_and_rollback(self):
        project = self.root / "project"
        relative = ".dsh/skills/demo/scripts/helper.sh"
        script = b"#!/bin/sh\nprintf 'migrated-script-ok\\n'\n"
        files = [PlannedFile(relative, script, root="project:example", mode=0o700)]
        result = self.engine.apply(self.project_plan(project, files))
        destination = project / relative
        self.assertEqual(destination.stat().st_mode & 0o7777, 0o700)
        process = subprocess.run([str(destination)], check=True, capture_output=True, text=True)
        self.assertEqual(process.stdout, "migrated-script-ok\n")
        self.assertEqual(self.engine.apply(self.project_plan(project, files))["status"], "unchanged")
        self.engine.rollback(Path(result["manifest_path"]))
        self.assertFalse(destination.exists())

    def test_same_bytes_executable_repair_is_backed_up_and_restores_mode(self):
        project = self.root / "project"
        relative = ".dsh/skills/demo/scripts/helper.sh"
        destination = project / relative
        destination.parent.mkdir(parents=True)
        content = b"#!/bin/sh\nexit 0\n"
        destination.write_bytes(content)
        destination.chmod(0o600)
        files = [PlannedFile(relative, content, root="project:example", mode=0o700)]
        plan = self.project_plan(project, files)
        entry = next(file for file in plan.to_dict()["files"] if file["path"] == relative)
        self.assertEqual(entry["action"], "update")
        self.assertEqual(entry["sha256"], entry["previous_sha256"])
        self.assertEqual(entry["mode"], 0o700)
        self.assertEqual(entry["previous_mode"], 0o600)
        result = self.engine.apply(plan)
        self.assertEqual(destination.stat().st_mode & 0o7777, 0o700)
        rolled = self.engine.rollback(Path(result["manifest_path"]))
        self.assertEqual(rolled["restored"], 1)
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(destination.stat().st_mode & 0o7777, 0o600)

    def test_identical_plain_document_keeps_existing_read_permissions(self):
        project = self.root / "project"
        project.mkdir()
        destination = project / "AGENTS.md"
        destination.write_bytes(b"same rules")
        destination.chmod(0o644)
        plan = self.project_plan(project, [PlannedFile("AGENTS.md", b"same rules", root="project:example")])
        entry = next(file for file in plan.to_dict()["files"] if file["path"] == "AGENTS.md")
        self.assertEqual(entry["action"], "unchanged")
        result = self.engine.apply(plan)
        self.assertEqual(destination.stat().st_mode & 0o7777, 0o644)
        self.engine.rollback(Path(result["manifest_path"]))
        self.assertEqual(destination.stat().st_mode & 0o7777, 0o644)

    def test_requested_modes_only_allow_owner_read_write_or_execute(self):
        for mode in (0, 0o400, 0o640, 0o755, 0o777, 0o4700):
            with self.subTest(mode=oct(mode)), self.assertRaises(MigrationError):
                self.prepare([PlannedFile("bad-mode", b"content", mode=mode)])
        self.assertFalse(self.home.exists())

    def test_permission_change_since_preview_is_preserved(self):
        project = self.root / "project"
        project.mkdir()
        destination = project / "script.sh"
        destination.write_bytes(b"same script")
        destination.chmod(0o600)
        plan = self.project_plan(project, [PlannedFile("script.sh", b"same script", root="project:example", mode=0o700)])
        destination.chmod(0o640)
        with self.assertRaises(ConflictError):
            self.engine.apply(plan)
        self.assertEqual(destination.stat().st_mode & 0o7777, 0o640)
        self.assertFalse(self.home.exists())

    def test_rollback_refuses_user_permission_edits_before_other_files(self):
        project = self.root / "project"
        result = self.engine.apply(self.project_plan(project, [
            PlannedFile("script.sh", b"same script", root="project:example", mode=0o700),
            PlannedFile("notes.md", b"notes", root="project:example"),
        ]))
        destination = project / "script.sh"
        destination.chmod(0o740)
        with self.assertRaises(ConflictError):
            self.engine.rollback(Path(result["manifest_path"]))
        self.assertEqual(destination.stat().st_mode & 0o7777, 0o740)
        self.assertEqual((project / "notes.md").read_bytes(), b"notes")

    def test_interrupted_mode_only_write_is_restored_from_intent_journal(self):
        project = self.root / "project"
        project.mkdir()
        destination = project / "script.sh"
        destination.write_bytes(b"same script")
        destination.chmod(0o600)
        plan = self.project_plan(project, [PlannedFile("script.sh", b"same script", root="project:example", mode=0o700)])
        from migrate_agent import engine
        actual_write = engine._atomic_write

        def fail_after_replace(path, content, mode=0o600):
            actual_write(path, content, mode)
            if path == destination and mode == 0o700:
                raise OSError("interrupted after executable replace")

        with mock.patch.object(engine, "_atomic_write", side_effect=fail_after_replace), self.assertRaises(OSError):
            self.engine.apply(plan)
        self.assertEqual(destination.read_bytes(), b"same script")
        self.assertEqual(destination.stat().st_mode & 0o7777, 0o600)
        manifest = next((self.home / ".migrate-agent/runs").glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text())["status"], "failed_rolled_back")


if __name__ == "__main__":
    unittest.main()
