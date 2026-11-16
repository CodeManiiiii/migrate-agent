import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from migrate_agent.cli import main
from migrate_agent.models import Bundle, Message, PlannedFile, Session, SourceOptions, TargetOptions, TargetPlan
from migrate_agent.registry import AdapterRegistry
from migrate_agent.service import MigrationService


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "codex"
        self.target = self.root / "dsh"
        self.project = self.root / "project"
        self.project.mkdir()
        sessions = self.source / "sessions" / "2026" / "11" / "16"
        sessions.mkdir(parents=True)
        (self.source / "AGENTS.md").write_text("Always explain migration decisions.\n", encoding="utf-8")
        rows = [
            {"timestamp": "2026-11-16T01:00:00Z", "type": "session_meta", "payload": {"id": "codex-test-1", "cwd": str(self.project), "timestamp": "2026-11-16T01:00:00Z"}},
            {"timestamp": "2026-11-16T01:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "继续迁移功能"}]}},
            {"timestamp": "2026-11-16T01:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "迁移器已完成"}]}},
        ]
        (sessions / "rollout-codex-test-1.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        self.flags = ["--source-home", str(self.source), "--target-home", str(self.target), "--json"]

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_preview_is_read_only_and_apply_can_rollback(self):
        code, output, error = self.run_cli("plan", *self.flags)
        self.assertEqual(code, 0, error)
        preview = json.loads(output)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["summary"]["sessions"], 1)
        self.assertFalse(self.target.exists())
        self.assertNotIn("继续迁移功能", output)

        source_bytes = {path: path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        code, output, error = self.run_cli("migrate", *self.flags)
        self.assertEqual(code, 0, error)
        applied = json.loads(output)
        self.assertEqual(applied["status"], "completed")
        self.assertTrue(applied["manifest_path"])
        self.assertTrue(list((self.target / "sessions").rglob("session.v4.jsonl*")))
        self.assertEqual(source_bytes, {path: path.read_bytes() for path in self.source.rglob("*") if path.is_file()})

        code, output, error = self.run_cli("migrate", *self.flags)
        self.assertEqual(code, 0, error)
        repeated = json.loads(output)
        self.assertEqual(repeated["summary"]["create"], 0)
        self.assertEqual(repeated["summary"]["update"], 0)

        code, output, error = self.run_cli("rollback", applied["manifest_path"], "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "rolled_back")
        self.assertFalse(list((self.target / "sessions").rglob("session.v4.jsonl*")))

    def test_alias_and_dry_run(self):
        code, output, error = self.run_cli("migrate", *self.flags, "--to", "deepseekharness", "--dry-run")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "preview")
        self.assertFalse(self.target.exists())

    def test_one_click_installs_native_project_rules_skills_and_can_restore_them(self):
        (self.project / ".git").mkdir()
        original = "Ignored original base rule\n"
        (self.project / "AGENTS.md").write_text(original)
        (self.project / "AGENTS.override.md").write_text("Effective override sentinel\n")
        skill = self.project / ".codex" / "skills" / "migration-review"
        (skill / "assets").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: migration-review\ndescription: Review project migrations\n---\nProject skill body sentinel\n")
        (skill / "assets" / "sample.bin").write_bytes(b"\x00\xff\x80")
        before_source = {path: path.read_bytes() for path in self.source.rglob("*") if path.is_file()}

        code, output, error = self.run_cli("plan", *self.flags)
        self.assertEqual(code, 0, error)
        preview = json.loads(output)
        self.assertGreaterEqual(preview["summary"]["projects"], 1)
        self.assertEqual((self.project / "AGENTS.md").read_text(), original)
        self.assertFalse((self.project / ".dsh").exists())

        code, output, error = self.run_cli("migrate", *self.flags)
        self.assertEqual(code, 0, error)
        first = json.loads(output)
        installed_rules = (self.project / "AGENTS.md").read_text()
        self.assertIn("Effective override sentinel", installed_rules)
        self.assertNotIn("Ignored original base rule", installed_rules)
        active_skill = self.project / ".dsh" / "skills" / "migration-review"
        self.assertIn("Project skill body sentinel", (active_skill / "SKILL.md").read_text())
        self.assertEqual((active_skill / "assets" / "sample.bin").read_bytes(), b"\x00\xff\x80")
        self.assertEqual(before_source, {path: path.read_bytes() for path in self.source.rglob("*") if path.is_file()})

        code, output, error = self.run_cli("migrate", *self.flags)
        self.assertEqual(code, 0, error)
        repeated = json.loads(output)
        self.assertEqual((self.project / "AGENTS.md").read_text(), installed_rules)
        self.assertEqual(len(list((self.target / "sessions").rglob("session.v4.jsonl*"))), 1)
        if repeated["manifest_path"]:
            code, _, error = self.run_cli("rollback", repeated["manifest_path"], "--json")
            self.assertEqual(code, 0, error)
        code, _, error = self.run_cli("rollback", first["manifest_path"], "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual((self.project / "AGENTS.md").read_text(), original)
        self.assertFalse((active_skill / "SKILL.md").exists())
        self.assertIn("Project skill body sentinel", (skill / "SKILL.md").read_text())

    def test_mapped_project_keeps_source_and_preserves_existing_target_policy(self):
        (self.project / "AGENTS.md").write_text("Source project policy\n")
        destination = self.root / "mapped-project"
        destination.mkdir()
        (destination / ".git").mkdir()
        (destination / "AGENTS.md").write_text("Existing target policy\n")
        code, output, error = self.run_cli("migrate", *self.flags, "--project", str(self.project), "--project-map", str(self.project) + "=" + str(destination))
        self.assertEqual(code, 0, error)
        rules = (destination / "AGENTS.md").read_text()
        self.assertIn("Existing target policy", rules)
        self.assertIn("Source project policy", rules)
        self.assertEqual((self.project / "AGENTS.md").read_text(), "Source project policy\n")
        code, _, error = self.run_cli("rollback", json.loads(output)["manifest_path"], "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual((destination / "AGENTS.md").read_text(), "Existing target policy\n")

    def test_config_only_project_is_unchanged_on_immediate_second_migration(self):
        (self.project / ".git").mkdir()
        config_dir = self.project / ".codex"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('developer_instructions="Config-only project rule"\n')
        parser = types.SimpleNamespace(load=lambda stream: {"developer_instructions": "Config-only project rule"})
        with patch.dict(sys.modules, {"tomllib": parser, "tomli": parser}):
            code, output, error = self.run_cli("migrate", *self.flags)
            self.assertEqual(code, 0, error)
            self.assertIn("Config-only project rule", (self.project / "AGENTS.md").read_text())
            code, output, error = self.run_cli("migrate", *self.flags)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["status"], "unchanged")

    def test_unsupported_target_is_clear_error(self):
        code, output, error = self.run_cli("plan", *self.flags, "--to", "unknown")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Unsupported target", json.loads(error)["error"])

    def test_unreadable_requested_session_does_not_import_unrelated_documents(self):
        code, output, error = self.run_cli("migrate", *self.flags, "--session", "missing-id")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("指定会话没有可读取的历史", json.loads(error)["error"])
        self.assertFalse(self.target.exists())

    def test_nested_roots_and_invalid_mapping_rejected(self):
        for extra in (["--target-home", str(self.source / "nested")], ["--project-map", "invalid"]):
            code, output, error = self.run_cli("plan", *self.flags, *extra)
            self.assertEqual(code, 1)
            self.assertEqual(output, "")
            self.assertEqual(json.loads(error)["status"], "error")

    def test_registry_supports_new_pair_without_pair_specific_engine(self):
        class Reader:
            name = "new-source"

            def read(self, options):
                return Bundle(source=self.name, sessions=[Session("one", "Title", "/repo", [Message("user", "hello")])])

        class Writer:
            name = "new-target"

            def plan(self, bundle, options):
                return TargetPlan(files=[PlannedFile("history.txt", bundle.sessions[0].messages[0].content.encode())])

        registry = AdapterRegistry()
        registry.register_source(Reader())
        registry.register_target(Writer())
        service = MigrationService(registry)
        plan = service.prepare("new-source", "new-target", SourceOptions(self.source), TargetOptions(self.target))
        self.assertEqual(plan.to_dict()["summary"]["sessions"], 1)
        result = service.engine.apply(plan)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.target / "history.txt").read_text(), "hello")


if __name__ == "__main__":
    unittest.main()
