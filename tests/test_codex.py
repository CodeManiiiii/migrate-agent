import base64
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from migrate_agent.adapters.codex import CodexSource
from migrate_agent.models import SourceOptions


ACTIVE = "11111111-1111-4111-8111-111111111111"
ARCHIVED = "22222222-2222-4222-8222-222222222222"


class CodexSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / ".codex"
        self.home.mkdir()

    def record(self, kind, payload, timestamp="2026-11-16T01:00:00Z"):
        return {"timestamp": timestamp, "type": kind, "payload": payload}

    def rollout(self, session_id=ACTIVE, archived=False, records=None):
        directory = self.home / ("archived_sessions" if archived else "sessions") / "2026"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("rollout-2026-11-16-" + session_id + ".jsonl")
        rows = [self.record("session_meta", {"id": session_id, "cwd": "/project", "timestamp": "2026-11-16T01:00:00Z"})]
        rows.extend(records if records is not None else [self.record("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Continue implementing migration"}]})])
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def history(self, session_id=ACTIVE, archived=0):
        state = sqlite3.connect(self.home / "state_5.sqlite")
        state.execute("CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT, archived INTEGER, created_at INTEGER, updated_at INTEGER)")
        state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", (session_id, str(self.home / "sessions" / (session_id + ".jsonl")), "/project/sqlite", "SQLite session", archived, 100, 200))
        state.commit()
        state.close()
        history = sqlite3.connect(self.home / "thread_history_1.sqlite")
        history.execute("CREATE TABLE IF NOT EXISTS thread_items(thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT)")
        items = [
            {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "Question"}]},
            {"type": "commandExecution", "id": "tool1", "command": "pytest", "aggregatedOutput": "all passed", "exitCode": 0},
            {"type": "agentMessage", "id": "a1", "text": "Answer"},
        ]
        # Insert backwards: imported order must follow rollout ordinal.
        for ordinal, item in reversed(list(enumerate(items))):
            history.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?)", (session_id, "t1", item["id"], ordinal, 100000 + ordinal, json.dumps(item)))
        history.commit()
        history.close()

    def read(self, **kwargs):
        return CodexSource().read(SourceOptions(home=self.home, **kwargs))

    def test_one_click_discovers_project_rules_and_skills_from_selected_history(self):
        project = self.root / "repository"
        (project / ".git").mkdir(parents=True)
        (project / "src").mkdir()
        (project / "AGENTS.md").write_text("Project rule sentinel")
        skill = project / ".codex" / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: review\ndescription: Review changes\n---\nSkill sentinel")
        script = skill / "check.sh"
        script.write_text("#!/bin/sh\nprintf 'script sentinel'\n")
        script.chmod(0o755)
        self.rollout(records=[
            self.record("session_meta", {"id": ACTIVE, "cwd": str(project / "src")}),
            self.record("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Continue"}]}),
        ])
        bundle = self.read()
        docs = {document.path: document for document in bundle.documents}
        self.assertEqual(docs["AGENTS.md"].project, str(project.resolve()))
        self.assertEqual(docs[".codex/skills/review/SKILL.md"].kind, "skill")
        self.assertTrue(docs[".codex/skills/review/check.sh"].executable)
        self.assertEqual(self.read(discover_projects=False).documents, [])
        self.assertEqual(self.read(include_project_docs=False).documents, [])

    def test_markerless_project_with_local_agent_files_is_discovered(self):
        project = self.root / "plain-project"
        project.mkdir()
        (project / "AGENTS.override.md").write_text("Standalone rule")
        self.rollout(records=[
            self.record("session_meta", {"id": ACTIVE, "cwd": str(project)}),
            self.record("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Continue"}]}),
        ])
        self.assertEqual([document.path for document in self.read().documents], ["AGENTS.override.md"])

    def test_overlapping_project_selections_do_not_duplicate_physical_documents(self):
        project = self.root / "repository"
        nested = project / "nested"
        nested.mkdir(parents=True)
        (nested / "AGENTS.md").write_text("Nested rule")
        bundle = self.read(projects=(project, nested))
        self.assertEqual(len(bundle.documents), 1)
        self.assertEqual(bundle.documents[0].path, "nested/AGENTS.md")

    def test_repeated_source_scan_excludes_generated_harness_rules_and_skill_outputs(self):
        project = self.root / "repository"
        (project / ".dsh" / "skills" / "example").mkdir(parents=True)
        (project / ".dsh" / "skills" / "example" / "AGENTS.md").write_text("Generated skill support")
        original = "Original source rule\n"
        (project / "AGENTS.md").write_text(original + "\n<!-- migrate-agent:codex:start -->\nGenerated config instructions\n<!-- migrate-agent:codex:end -->\n")
        bundle = self.read(projects=(project,))
        self.assertEqual(len(bundle.documents), 1)
        self.assertEqual(bundle.documents[0].content, original)
        self.assertIn("Generated config instructions", (project / "AGENTS.md").read_text())

    def test_nonempty_override_exports_only_effective_project_rule(self):
        project = self.root / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("Ignored base rule")
        override = project / "AGENTS.override.md"
        override.write_text("Effective source rule")
        docs = self.read(projects=(project,)).documents
        self.assertEqual([document.path for document in docs], ["AGENTS.override.md"])
        override.write_text("  \n")
        docs = self.read(projects=(project,)).documents
        self.assertIn("AGENTS.md", [document.path for document in docs])

    def test_project_instruction_identity_survives_managed_merge_without_final_newline(self):
        project = self.root / "project"
        project.mkdir()
        rule = project / "AGENTS.md"
        rule.write_text("Original instruction without newline")
        before = self.read(projects=(project,)).documents[0].content
        rule.write_text("Original instruction without newline\n\n<!-- migrate-agent:codex:start -->\nGenerated config\n<!-- migrate-agent:codex:end -->\n")
        after = self.read(projects=(project,)).documents[0].content
        self.assertEqual(before, after)

    def test_project_scan_does_not_reimport_custom_source_home_inside_project(self):
        (self.home / "AGENTS.md").write_text("Codex global rule")
        (self.root / "AGENTS.md").write_text("Project rule")
        bundle = self.read(projects=(self.root,))
        global_rules = [document for document in bundle.documents if document.scope == "global"]
        project_rules = [document for document in bundle.documents if document.scope == "project"]
        self.assertEqual([document.path for document in global_rules], ["AGENTS.md"])
        self.assertEqual([document.path for document in project_rules], ["AGENTS.md"])
        self.assertEqual(project_rules[0].content, "Project rule\n")

    def test_native_history_keeps_tools_summary_and_does_not_duplicate_ui_events(self):
        rows = [
            self.record("event_msg", {"type": "user_message", "message": "Question"}),
            self.record("response_item", {"type": "message", "id": "u1", "role": "user", "content": [{"type": "input_text", "text": "Question"}]}),
            self.record("response_item", {"type": "function_call", "id": "c1", "call_id": "call1", "name": "exec", "arguments": '{"cmd":"pytest"}'}),
            self.record("response_item", {"type": "function_call_output", "call_id": "call1", "output": "all passed"}),
            self.record("event_msg", {"type": "item_completed", "item": {"type": "agentMessage", "id": "a1", "text": "Answer"}}),
            self.record("response_item", {"type": "message", "id": "a1", "role": "assistant", "content": [{"type": "output_text", "text": "Answer"}]}),
            self.record("compacted", {"message": "Keep the migration design", "replacement_history": []}),
        ]
        self.rollout(records=rows)
        bundle = self.read()
        session = bundle.sessions[0]
        self.assertEqual([m.content for m in session.messages], ["Question", '{"cmd":"pytest"}', "all passed", "Answer", "Keep the migration design"])
        self.assertEqual(session.messages[1].call_id, "call1")
        self.assertEqual(session.messages[-1].kind, "summary")
        self.assertEqual(len(session.records), len(rows) + 1)

    def test_legacy_events_interleave_with_native_tool_records(self):
        self.rollout(records=[
            self.record("event_msg", {"type": "user_message", "message": "Question"}),
            self.record("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "1", "input": "command"}),
            self.record("response_item", {"type": "custom_tool_call_output", "call_id": "1", "output": "result"}),
            self.record("event_msg", {"type": "agent_message", "message": "Answer"}),
        ])
        self.assertEqual([m.content for m in self.read().sessions[0].messages], ["Question", "command", "result", "Answer"])

    def test_sqlite_only_history_retains_order_metadata_and_tools(self):
        self.history()
        bundle = self.read()
        self.assertEqual(len(bundle.sessions), 1)
        session = bundle.sessions[0]
        self.assertEqual(session.id, ACTIVE)
        self.assertEqual(session.cwd, "/project/sqlite")
        self.assertEqual(session.title, "SQLite session")
        self.assertEqual([m.content for m in session.messages], ["Question", "pytest", "all passed", "Answer"])
        self.assertEqual(len(session.records), 3)
        self.assertTrue(any("SQLite" in warning for warning in bundle.warnings))

    def test_copied_checkpointed_wal_database_needs_no_source_sidecars(self):
        self.history()
        for path in self.home.glob("*.sqlite"):
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.home.iterdir()}
        bundle = self.read()
        self.assertEqual(len(bundle.sessions), 1)
        self.assertEqual(bundle.sessions[0].title, "SQLite session")
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.home.iterdir()}
        self.assertEqual(before, after)

    def test_existing_pending_wal_is_read_rather_than_ignored(self):
        self.history()
        path = self.home / "state_5.sqlite"
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE threads SET title='Pending WAL title' WHERE id=?", (ACTIVE,))
        writer.commit()
        self.assertTrue(path.with_name(path.name + "-wal").is_file())
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.home.iterdir()}
        self.assertEqual(self.read().sessions[0].title, "Pending WAL title")
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.home.iterdir()}
        self.assertEqual(before, after)

    def test_unstable_live_wal_snapshot_is_reported_and_skipped(self):
        self.history()
        path = self.home / "state_5.sqlite"
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE threads SET title='Live source' WHERE id=?", (ACTIVE,))
        writer.commit()
        original_copy = shutil.copyfile
        writes = []

        def copy_and_modify(source, destination, *args, **kwargs):
            result = original_copy(source, destination, *args, **kwargs)
            if Path(source).resolve() == path.resolve():
                writes.append(len(writes))
                writer.execute("UPDATE threads SET title=? WHERE id=?", ("Changing source " + str(len(writes)), ACTIVE))
                writer.commit()
            return result

        with patch("migrate_agent.adapters.codex.shutil.copyfile", side_effect=copy_and_modify):
            bundle = self.read()
        self.assertEqual(len(writes), 3)
        self.assertTrue(any("all 3 SQLite snapshot attempts" in warning for warning in bundle.warnings))
        # The readable conversation history can still be imported; unstable
        # optional metadata is never silently treated as a consistent snapshot.
        self.assertEqual(len(bundle.sessions), 1)

    def test_copied_pending_wal_without_shm_uses_temporary_sidecars(self):
        self.history()
        original = self.home / "state_5.sqlite"
        writer = sqlite3.connect(original)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE threads SET title='Copied pending WAL' WHERE id=?", (ACTIVE,))
        writer.commit()
        copied_home = self.root / "copied-codex"
        copied_home.mkdir()
        for path in self.home.iterdir():
            if path.is_file() and not path.name.endswith("-shm"):
                shutil.copyfile(path, copied_home / path.name)
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in copied_home.iterdir()}
        bundle = CodexSource().read(SourceOptions(home=copied_home))
        self.assertEqual(bundle.sessions[0].title, "Copied pending WAL")
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in copied_home.iterdir()}
        self.assertEqual(before, after)

    def test_filters_include_archived_and_requested_sessions(self):
        self.rollout()
        self.rollout(ARCHIVED, archived=True)
        self.assertEqual([s.id for s in self.read(include_archived=False).sessions], [ACTIVE])
        self.assertEqual([s.id for s in self.read(session_ids=(ARCHIVED,)).sessions], [ARCHIVED])
        missing = self.read(session_ids=("not-found",))
        self.assertEqual(missing.sessions, [])
        self.assertTrue(any("not-found" in warning for warning in missing.warnings))

    def test_sqlite_archived_filter(self):
        self.history(ARCHIVED, archived=1)
        self.assertEqual(self.read(include_archived=False).sessions, [])
        self.assertTrue(self.read().sessions[0].archived)

    def test_native_history_takes_priority_over_sqlite_projection(self):
        self.rollout(records=[
            self.record("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Native question"}]}),
            self.record("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Native answer"}]}),
        ])
        self.history()
        session = self.read().sessions[0]
        self.assertEqual([m.content for m in session.messages], ["Native question", "Native answer"])
        self.assertEqual(session.title, "SQLite session")
        self.assertEqual(session.cwd, "/project/sqlite")

    def test_partial_native_rollout_uses_sqlite_with_instructions_and_summary(self):
        self.rollout(records=[
            self.record("response_item", {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Native instructions"}]}),
            self.record("compacted", {"message": "Previous progress"}),
            self.record("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Question"}]}),
        ])
        self.history()
        bundle = self.read()
        contents = [m.content for m in bundle.sessions[0].messages]
        self.assertIn("Native instructions", contents)
        self.assertIn("Previous progress", contents)
        self.assertEqual(contents.count("Question"), 1)
        self.assertIn("Answer", contents)
        self.assertTrue(any("partial native rollout" in warning for warning in bundle.warnings))

    def test_project_filter_matches_descendants_not_same_prefix(self):
        project = self.root / "project"
        project.mkdir()
        first = self.rollout()
        rows = first.read_text().splitlines()
        meta = json.loads(rows[0])
        meta["payload"]["cwd"] = str(project / "nested")
        rows[0] = json.dumps(meta)
        first.write_text("\n".join(rows) + "\n")
        second = self.rollout(ARCHIVED)
        rows = second.read_text().splitlines()
        meta = json.loads(rows[0])
        meta["payload"]["cwd"] = str(project) + "-other"
        rows[0] = json.dumps(meta)
        second.write_text("\n".join(rows) + "\n")
        self.assertEqual([s.id for s in self.read(projects=(project,)).sessions], [ACTIVE])

    def test_project_filter_omits_unrelated_sqlite_sessions_without_missing_warning(self):
        self.history()
        project = self.root / "project"
        project.mkdir()
        bundle = self.read(projects=(project,))
        self.assertEqual(bundle.sessions, [])
        self.assertFalse(any(ACTIVE in warning for warning in bundle.warnings))

    def test_pascal_case_ui_projection_without_native_messages(self):
        self.rollout(records=[self.record("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "Question"}]}})])
        self.assertEqual(self.read().sessions[0].messages[0].content, "Question")

    def test_malformed_trailing_json_keeps_complete_history(self):
        path = self.rollout()
        with path.open("a") as stream:
            stream.write('{"partial":')
        bundle = self.read()
        self.assertEqual(len(bundle.sessions[0].messages), 1)
        self.assertTrue(any("malformed" in warning for warning in bundle.warnings))

    def test_skills_binary_assets_project_context_and_boundary(self):
        (self.home / "AGENTS.md").write_text("Global rules", encoding="utf-8")
        skill = self.home / "skills" / "custom"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Custom skill", encoding="utf-8")
        (skill / "script.py").write_text("print('helper')", encoding="utf-8")
        (skill / "image.png").write_bytes(b"\x89PNG\x00\xff")
        builtin = self.home / "skills" / ".system"
        builtin.mkdir()
        (builtin / "SKILL.md").write_text("Built in")
        outside = self.root / "private.md"
        outside.write_text("Outside")
        (skill / "escape.md").symlink_to(outside)
        shared = self.root / ".agents" / "skills" / "shared"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text("Shared skill")
        project = self.root / "project"
        (project / "nested").mkdir(parents=True)
        (project / "AGENTS.md").write_text("Project rules")
        (project / "nested" / "AGENTS.md").write_text("Nested rules")
        (project / ".codex" / "skills" / "project-skill").mkdir(parents=True)
        (project / ".codex" / "skills" / "project-skill" / "SKILL.md").write_text("Project skill")
        bundle = self.read(projects=(project,))
        documents = {d.path: d for d in bundle.documents}
        self.assertNotIn("skills/escape.md", documents)
        self.assertNotIn("skills/.system/SKILL.md", documents)
        self.assertIn("skills/custom/script.py", documents)
        self.assertEqual(base64.b64decode(documents["skills/custom/image.png"].content), b"\x89PNG\x00\xff")
        self.assertEqual(documents["skills/custom/image.png"].encoding, "base64")
        self.assertEqual(documents["nested/AGENTS.md"].project, str(project.resolve()))
        self.assertEqual(documents[".codex/skills/project-skill/SKILL.md"].kind, "skill")
        self.assertEqual(documents["skills/shared/SKILL.md"].scope, "shared")
        self.assertTrue(any("symlink" in warning for warning in bundle.warnings))

    def test_no_skills_flag_omits_project_and_global_skills(self):
        skill = self.home / "skills" / "custom"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Skill")
        project = self.root / "project"
        (project / ".codex" / "skills" / "custom").mkdir(parents=True)
        (project / ".codex" / "skills" / "custom" / "SKILL.md").write_text("Skill")
        self.assertEqual(self.read(projects=(project,), include_skills=False).documents, [])

    def test_session_index_latest_name_wins(self):
        self.rollout()
        index = self.home / "session_index.jsonl"
        index.write_text(json.dumps({"id": ACTIVE, "thread_name": "Old name"}) + "\n" + json.dumps({"id": ACTIVE, "thread_name": "New name"}) + "\n")
        self.assertEqual(self.read().sessions[0].title, "New name")

    @unittest.skipUnless(shutil.which("zstd"), "zstd command not installed")
    def test_compressed_rollout_with_space_in_path(self):
        path = self.rollout()
        compressed = path.with_suffix(".jsonl.zst")
        subprocess.run([shutil.which("zstd"), "-q", "-f", str(path), "-o", str(compressed)], check=True)
        path.unlink()
        bundle = self.read()
        self.assertEqual(len(bundle.sessions), 1)
        self.assertEqual(bundle.sessions[0].id, ACTIVE)

    def test_missing_decoder_falls_back_to_sqlite_and_warns(self):
        self.history()
        folder = self.home / "sessions"
        folder.mkdir()
        (folder / ("rollout-" + ACTIVE + ".jsonl.zst")).write_bytes(b"invalid zstd")
        with patch.dict(sys.modules, {"zstandard": None, "compression": None}), patch("migrate_agent.adapters.codex.shutil.which", return_value=None):
            bundle = self.read()
        self.assertEqual(len(bundle.sessions), 1)
        self.assertTrue(any("decompression unavailable" in warning for warning in bundle.warnings))

    def test_config_is_allowlisted_and_auth_is_never_read(self):
        (self.home / "config.toml").write_text('model="codex-model"\n[mcp_servers.private]\ncommand="private-command"\nenv={SECRET="do-not-export"}\n')
        (self.home / "auth.json").write_text('{"token":"do-not-export-auth"}')
        bundle = self.read()
        serialized = json.dumps(bundle.to_dict())
        self.assertNotIn("do-not-export", serialized)
        self.assertNotIn("private-command", serialized)
        # On Python 3.9 without tomli, the importer reports its optional parser
        # requirement instead of copying raw configuration into the bundle.
        self.assertTrue(bundle.settings.get("model") == "codex-model" or any("tomli" in warning for warning in bundle.warnings))

    def test_native_attachments_have_placeholders_preserved_records_and_warning(self):
        image_data = "data:image/png;base64,ZmFrZQ=="
        self.rollout(records=[self.record("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Inspect these attachments"},
            {"type": "input_image", "image_url": image_data},
            {"type": "localImage", "path": "/original/diagram.png"},
            {"type": "input_file", "file_id": "file-123"},
        ]})])
        attachments = self.home / "attachments"
        attachments.mkdir()
        (attachments / "secret-image.png").write_bytes(b"image bytes")
        bundle = self.read()
        content = bundle.sessions[0].messages[0].content
        self.assertIn("[Embedded image retained in source records]", content)
        self.assertIn("[Attachment: /original/diagram.png]", content)
        self.assertIn("[Attachment: file-123]", content)
        self.assertNotIn(image_data, content)
        self.assertIn(image_data, json.dumps(bundle.sessions[0].records))
        self.assertEqual(bundle.documents, [])
        self.assertTrue(any(ACTIVE in warning and "attachments" in warning and "not copied" in warning for warning in bundle.warnings))

    def test_reasoning_only_exposes_public_summary(self):
        self.rollout(records=[
            self.record("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Question"}]}),
            self.record("response_item", {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Public summary"}], "content": [{"type": "reasoning_text", "text": "Private internal reasoning"}], "encrypted_content": "opaque cipher text"}),
        ])
        session = self.read().sessions[0]
        self.assertEqual([m.content for m in session.messages], ["Question", "Public summary"])
        self.assertNotIn("Private internal reasoning", "\n".join(m.content for m in session.messages))
        self.assertEqual(session.messages[-1].kind, "reasoning_summary")

    def test_config_user_instructions_are_documents_with_boundary_checks(self):
        (self.home / "config.toml").write_text('developer_instructions="Custom user directions"\nmodel_instructions_file="custom.md"\n')
        (self.home / "custom.md").write_text("Custom model instructions")
        config = {"developer_instructions": "Custom user directions", "model_instructions_file": "custom.md"}
        parser = types.SimpleNamespace(load=lambda stream: config)
        with patch.dict(sys.modules, {"tomllib": parser, "tomli": parser}):
            bundle = self.read()
        documents = {document.path: document for document in bundle.documents}
        self.assertEqual(documents["config/developer_instructions.md"].content, "Custom user directions")
        self.assertEqual(documents["config/model_instructions.md"].content, "Custom model instructions")
        outside = self.root / "outside.md"
        outside.write_text("Outside boundary")
        config["model_instructions_file"] = str(outside)
        with patch.dict(sys.modules, {"tomllib": parser, "tomli": parser}):
            bundle = self.read()
        self.assertNotIn("Outside boundary", json.dumps(bundle.to_dict()))
        self.assertTrue(any("outside selected source roots" in warning for warning in bundle.warnings))

    def test_project_and_nested_config_instructions_keep_their_project_scope(self):
        project = self.root / "project"
        root_config = project / ".codex" / "config.toml"
        nested_root = project / "nested"
        nested_config = nested_root / ".codex" / "config.toml"
        for path in (root_config, nested_config):
            path.parent.mkdir(parents=True)
            path.write_text('developer_instructions="fixture"\n')
        (root_config.parent / "root-instructions.txt").write_text("Root custom model instructions")
        (nested_config.parent / "nested-instructions.txt").write_text("Nested custom model instructions")
        configs = {
            root_config.resolve(): {"developer_instructions": "Root project directions", "model_instructions_file": "root-instructions.txt", "model": "must-not-be-global", "mcp_servers": {"private": {"command": "never-execute", "env": {"KEY": "project-secret"}}}},
            nested_config.resolve(): {"developer_instructions": "Nested project directions", "model_instructions_file": "nested-instructions.txt", "sandbox_mode": "danger-full-access"},
        }
        parser = types.SimpleNamespace(load=lambda stream: configs[Path(stream.name).resolve()])
        with patch.dict(sys.modules, {"tomllib": parser, "tomli": parser}):
            bundle = self.read(projects=(project, nested_root))
        documents = {(d.project, d.path): d for d in bundle.documents}
        self.assertEqual(len(documents), 4)
        self.assertEqual(len(bundle.documents), 4)
        self.assertEqual(documents[(str(project.resolve()), "codex-config-developer_instructions.md")].content, "Root project directions")
        self.assertEqual(documents[(str(project.resolve()), "codex-config-model_instructions.md")].content, "Root custom model instructions")
        self.assertEqual(documents[(str(nested_root.resolve()), "codex-config-developer_instructions.md")].content, "Nested project directions")
        self.assertEqual(documents[(str(nested_root.resolve()), "codex-config-model_instructions.md")].content, "Nested custom model instructions")
        self.assertTrue(all(d.scope == "project" and d.kind == "instruction" and "/" not in d.path for d in bundle.documents))
        self.assertEqual(bundle.settings, {})
        serialized = json.dumps(bundle.to_dict())
        self.assertNotIn("project-secret", serialized)
        self.assertNotIn("never-execute", serialized)
        self.assertNotIn("danger-full-access", serialized)

    def test_project_config_custom_file_outside_project_is_reported_and_excluded(self):
        project = self.root / "project"
        config_path = project / ".codex" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text('model_instructions_file="../../outside.txt"\n')
        outside = self.root / "outside.txt"
        outside.write_text("Outside project instructions")
        parser = types.SimpleNamespace(load=lambda stream: {"model_instructions_file": "../../outside.txt"})
        with patch.dict(sys.modules, {"tomllib": parser, "tomli": parser}):
            bundle = self.read(projects=(project,))
        self.assertEqual(bundle.documents, [])
        self.assertNotIn("Outside project instructions", json.dumps(bundle.to_dict()))
        self.assertTrue(any("outside selected source roots" in warning for warning in bundle.warnings))

    def test_project_configs_honor_no_project_docs_and_parser_warning_is_once(self):
        project = self.root / "project"
        for config in (project / ".codex" / "config.toml", project / "nested" / ".codex" / "config.toml"):
            config.parent.mkdir(parents=True)
            config.write_text('developer_instructions="Project instructions"\n')
        with patch.dict(sys.modules, {"tomllib": None, "tomli": None}):
            bundle = self.read(projects=(project,))
            disabled = self.read(projects=(project,), include_project_docs=False)
        self.assertEqual(sum("requires optional tomli" in warning for warning in bundle.warnings), 1)
        self.assertEqual(disabled.documents, [])
        self.assertFalse(any("tomli" in warning for warning in disabled.warnings))


if __name__ == "__main__":
    unittest.main()
