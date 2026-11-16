"""Optional real-Harness integration; no model call or real user-data writes.

Set MIGRATE_AGENT_DSH_NODE_MODULES to an extracted installed Harness runtime's
node_modules directory. The regular suite skips this test when unavailable.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from migrate_agent.models import SourceOptions, TargetOptions
from migrate_agent.service import MigrationService


_NATIVE_MODULES = os.environ.get("MIGRATE_AGENT_DSH_NODE_MODULES", "")
_HAS_NATIVE_RUNTIME = bool(_NATIVE_MODULES and shutil.which("node") and Path(_NATIVE_MODULES).is_dir())


@unittest.skipUnless(_HAS_NATIVE_RUNTIME, "Set MIGRATE_AGENT_DSH_NODE_MODULES to verify against native Harness packages")
class NativeProjectIntegrationTests(unittest.TestCase):
    def test_mapped_rules_and_skills_are_discovered_in_fresh_native_sessions(self):
        with tempfile.TemporaryDirectory(prefix="migrate-agent-native-project-") as temporary:
            root = Path(temporary).resolve()
            source_home = root / "codex-home"
            source_home.mkdir()
            source = root / "old project"
            other_source = root / "old other project"
            target = root / "new project"
            other_target = root / "new other project"
            for project in (source, other_source, target, other_target):
                (project / ".git").mkdir(parents=True)
            (source / "child").mkdir()
            (target / "child").mkdir()
            (target / "sibling").mkdir()
            (source / "AGENTS.md").write_text("ROOT_SUPERSEDED_SENTINEL\n", encoding="utf-8")
            (source / "AGENTS.override.md").write_text("ROOT_EFFECTIVE_SENTINEL\n", encoding="utf-8")
            (source / "child" / "AGENTS.md").write_text("CHILD_RULE_SENTINEL\n", encoding="utf-8")
            (other_source / "AGENTS.md").write_text("OTHER_RULE_SENTINEL\n", encoding="utf-8")
            original_target = "TARGET_EXISTING_SENTINEL\n"
            (target / "AGENTS.md").write_text(original_target, encoding="utf-8")

            skill = source / ".codex" / "skills" / "project-helper"
            (skill / "assets").mkdir(parents=True)
            (skill / "scripts").mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: project-helper\ndescription: Use this project workflow\n---\n"
                "PROJECT_SKILL_SENTINEL\nRead assets/payload.bin and scripts/helper.py relative to this skill.\n",
                encoding="utf-8",
            )
            binary = b"\x00\xffproject-resource\x00"
            script = "print('resource-check-only')\n"
            shell_script = "#!/bin/sh\nprintf 'MIGRATED_SCRIPT_EXECUTED\\n'\n"
            (skill / "assets" / "payload.bin").write_bytes(binary)
            (skill / "scripts" / "helper.py").write_text(script, encoding="utf-8")
            (skill / "scripts" / "helper.sh").write_text(shell_script, encoding="utf-8")
            (skill / "scripts" / "helper.sh").chmod(0o755)
            nested_skill = source / ".codex" / "skills" / "group" / "nested"
            (nested_skill / "references").mkdir(parents=True)
            (nested_skill / "SKILL.md").write_text(
                "---\nname: NestedHelper\ndescription: Nested source skill converted to native layout\n---\n"
                "NESTED_SKILL_SENTINEL\nRead references/nested.txt relative to this skill.\n", encoding="utf-8"
            )
            (nested_skill / "references" / "nested.txt").write_text("Nested reference bytes\n", encoding="utf-8")
            human_only = source / ".codex" / "skills" / "human-only"
            human_only.mkdir(parents=True)
            (human_only / "SKILL.md").write_text(
                "---\nname: human-only\ndescription: Preserve user-only invocation\n"
                "disableModelInvocation: true\nuserInvocable: true\n---\nHUMAN_ONLY_SKILL_SENTINEL\n", encoding="utf-8"
            )
            other_skill = other_source / ".codex" / "skills" / "other-helper"
            other_skill.mkdir(parents=True)
            (other_skill / "SKILL.md").write_text(
                "---\nname: other-helper\ndescription: Another project's workflow\n---\nOTHER_SKILL_SENTINEL\n",
                encoding="utf-8",
            )
            config_expected = []
            if importlib.util.find_spec("tomllib") is not None or importlib.util.find_spec("tomli") is not None:
                (source / ".codex" / "config.toml").write_text(
                    'developer_instructions="PROJECT_CONFIG_SENTINEL"\n', encoding="utf-8"
                )
                config_expected = ["PROJECT_CONFIG_SENTINEL"]
            source_files = {
                path: path.read_bytes()
                for project in (source_home, source, other_source)
                for path in project.rglob("*") if path.is_file()
            }
            target_home = root / "isolated-dsh-home"
            options = TargetOptions(target_home, project_map={str(source): str(target), str(other_source): str(other_target)})
            service = MigrationService()
            plan = service.prepare(
                "codex", "deepseek", SourceOptions(source_home, projects=(source, other_source)),
                options,
            )
            self.assertFalse(target_home.exists(), "Preview must not create the Harness home")
            self.assertEqual((target / "AGENTS.md").read_text(), original_target)
            self.assertFalse((target / ".dsh").exists(), "Preview must not write project skill roots")
            report = service.engine.apply(plan)
            self.assertEqual(report["status"], "completed")

            expected_skill = {
                "name": "project-helper", "contentInclude": ["PROJECT_SKILL_SENTINEL"],
                "modelInvocable": True, "userInvocable": True,
                "resourceBase": str(target / ".dsh" / "skills" / "project-helper"),
                "resources": [
                    {"path": "assets/payload.bin", "sha256": hashlib.sha256(binary).hexdigest()},
                    {"path": "scripts/helper.py", "text": script},
                    {"path": "scripts/helper.sh", "text": shell_script, "executable": True},
                ],
            }
            shared_rules = ["ROOT_EFFECTIVE_SENTINEL", "TARGET_EXISTING_SENTINEL", *config_expected]
            project_skills = [expected_skill, {
                "name": "nested-helper", "contentInclude": ["NESTED_SKILL_SENTINEL"],
                "resourceBase": str(target / ".dsh" / "skills" / "group-nested"),
                "resources": [{"path": "references/nested.txt", "text": "Nested reference bytes\n"}],
            }, {
                "name": "human-only", "contentInclude": ["HUMAN_ONLY_SKILL_SENTINEL"],
                "modelInvocable": False, "userInvocable": True,
            }]
            cases = [
                {"cwd": str(target), "rulesInclude": shared_rules,
                 "ruleFiles": [str(target / "AGENTS.md")],
                 "rulesExclude": ["ROOT_SUPERSEDED_SENTINEL", "CHILD_RULE_SENTINEL", "OTHER_RULE_SENTINEL"],
                 "skills": project_skills, "skillsAbsent": ["other-helper"]},
                {"cwd": str(target / "child"), "rulesInclude": [*shared_rules, "CHILD_RULE_SENTINEL"],
                 "ruleFiles": [str(target / "AGENTS.md"), str(target / "child" / "AGENTS.md")],
                 "rulesOrder": ["ROOT_EFFECTIVE_SENTINEL", "CHILD_RULE_SENTINEL"],
                 "rulesExclude": ["ROOT_SUPERSEDED_SENTINEL", "OTHER_RULE_SENTINEL"],
                 "skills": project_skills, "skillsAbsent": ["other-helper"]},
                {"cwd": str(target / "sibling"), "rulesInclude": shared_rules,
                 "ruleFiles": [str(target / "AGENTS.md")],
                 "rulesExclude": ["CHILD_RULE_SENTINEL", "OTHER_RULE_SENTINEL"],
                 "skills": project_skills, "skillsAbsent": ["other-helper"]},
                {"cwd": str(other_target), "rulesInclude": ["OTHER_RULE_SENTINEL"],
                 "ruleFiles": [str(other_target / "AGENTS.md")],
                 "rulesExclude": ["ROOT_EFFECTIVE_SENTINEL", "CHILD_RULE_SENTINEL"],
                 "skills": [{"name": "other-helper", "contentInclude": ["OTHER_SKILL_SENTINEL"]}],
                 "skillsAbsent": ["project-helper", "nested-helper", "human-only"]},
            ]
            scenario = root / "scenario.json"
            scenario.write_text(json.dumps({
                "dshHome": str(target_home), "agentsHome": str(root / "isolated-agents-home"), "cases": cases,
            }), encoding="utf-8")
            check = subprocess.run(
                [shutil.which("node"), "--experimental-transform-types",
                 str(Path(__file__).resolve().parents[1] / "scripts" / "verify_deepseek_project.mjs"),
                 _NATIVE_MODULES, str(scenario)],
                check=False, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(check.returncode, 0, check.stderr)
            native = json.loads(check.stdout)
            self.assertEqual(native["status"], "verified")
            self.assertEqual(native["freshSessions"], 4)
            self.assertEqual(native["skillLoads"], 10)
            self.assertEqual(native["toolLoads"], 7)
            self.assertEqual(native["resourceChecks"], 12)
            executed = subprocess.run(
                [str(target / ".dsh" / "skills" / "project-helper" / "scripts" / "helper.sh")],
                check=True, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(executed.stdout, "MIGRATED_SCRIPT_EXECUTED\n")
            # The fixture has no sessions at all: only native new-session disk
            # discovery can provide the rules and skills asserted above.
            self.assertEqual(report["summary"]["sessions"], 0)
            repeated = service.prepare(
                "codex", "deepseek", SourceOptions(source_home, projects=(source, other_source)),
                options,
            )
            self.assertEqual(service.engine.apply(repeated)["status"], "unchanged")
            service.engine.rollback(Path(report["manifest_path"]))
            self.assertEqual((target / "AGENTS.md").read_text(), original_target)
            self.assertFalse((target / ".dsh").exists())
            self.assertEqual(source_files, {path: path.read_bytes() for path in source_files})

    def test_in_place_override_and_skill_load_in_a_new_session(self):
        with tempfile.TemporaryDirectory(prefix="migrate-agent-native-in-place-") as temporary:
            root = Path(temporary).resolve()
            source_home = root / "codex-home"
            source_home.mkdir()
            project = root / "project"
            (project / ".git").mkdir(parents=True)
            original_base = "IN_PLACE_SUPERSEDED_RULE\n"
            override = "IN_PLACE_EFFECTIVE_RULE\n"
            (project / "AGENTS.md").write_text(original_base, encoding="utf-8")
            (project / "AGENTS.override.md").write_text(override, encoding="utf-8")
            skill = project / ".codex" / "skills" / "in-place-helper"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: in-place-helper\ndescription: In-place migration check\n---\nIN_PLACE_SKILL_BODY\n",
                encoding="utf-8",
            )
            service = MigrationService()
            options = SourceOptions(source_home, projects=(project,))
            target_home = root / "isolated-dsh-home"
            target = TargetOptions(target_home)
            plan = service.prepare("codex", "deepseek", options, target)
            report = service.engine.apply(plan)
            scenario = root / "scenario.json"
            scenario.write_text(json.dumps({
                "dshHome": str(target_home), "agentsHome": str(root / "isolated-agents-home"),
                "cases": [{
                    "cwd": str(project), "rulesInclude": ["IN_PLACE_EFFECTIVE_RULE"],
                    "ruleFiles": [str(project / "AGENTS.md")],
                    "rulesExclude": ["IN_PLACE_SUPERSEDED_RULE"],
                    "skills": [{"name": "in-place-helper", "contentInclude": ["IN_PLACE_SKILL_BODY"]}],
                }],
            }), encoding="utf-8")
            checked = subprocess.run(
                [shutil.which("node"), "--experimental-transform-types",
                 str(Path(__file__).resolve().parents[1] / "scripts" / "verify_deepseek_project.mjs"),
                 _NATIVE_MODULES, str(scenario)],
                check=False, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(json.loads(checked.stdout)["freshSessions"], 1)
            self.assertEqual(json.loads(checked.stdout)["toolLoads"], 1)
            native_bytes = (project / "AGENTS.md").read_bytes()
            repeat = service.engine.apply(service.prepare("codex", "deepseek", options, target))
            self.assertEqual(repeat["status"], "unchanged")
            self.assertEqual((project / "AGENTS.md").read_bytes(), native_bytes)
            service.engine.rollback(Path(report["manifest_path"]))
            self.assertEqual((project / "AGENTS.md").read_text(), original_base)
            self.assertEqual((project / "AGENTS.override.md").read_text(), override)
            self.assertFalse((project / ".dsh").exists())


if __name__ == "__main__":
    unittest.main()
