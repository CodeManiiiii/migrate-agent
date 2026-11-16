import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from migrate_agent.adapters.deepseek import DeepSeekTarget, encode_segment, project_key, zstd_raw_frame
from migrate_agent.models import Bundle, Document, Message, Session, TargetOptions


def decode_raw_frames(data):
    """Small RFC 8878 raw-block reader used to inspect the planned artifacts."""
    offset = 0
    frames = []
    while offset < len(data):
        if data[offset:offset + 4] != b'\x28\xb5\x2f\xfd':
            raise AssertionError('Invalid Zstandard frame magic')
        offset += 4
        descriptor = data[offset]
        offset += 1
        if descriptor & 0x3F != 0x20:
            raise AssertionError('Expected single-segment raw frame')
        count = [1, 2, 4, 8][descriptor >> 6]
        size = int.from_bytes(data[offset:offset + count], 'little')
        size += 256 if count == 2 else 0
        offset += count
        frame = bytearray()
        while True:
            block = int.from_bytes(data[offset:offset + 3], 'little')
            offset += 3
            if (block >> 1) & 3:
                raise AssertionError('Expected raw block')
            length = block >> 3
            if length > 131072:
                raise AssertionError('Block too large')
            frame.extend(data[offset:offset + length])
            offset += length
            if block & 1:
                break
        if len(frame) != size:
            raise AssertionError('Content size mismatch')
        frames.append(bytes(frame))
    return frames


def session_rows(plan):
    artifact = next(file for file in plan.files if file.path.endswith('.zstd'))
    frames = decode_raw_frames(artifact.content)
    if len(frames) != 2 or frames[0].count(b'\n') != 1:
        raise AssertionError('Native Harness requires an independent header frame')
    return artifact, [json.loads(row) for frame in frames for row in frame.splitlines()]


class DeepSeekTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / '.dsh'
        self.options = TargetOptions(self.home)
        self.session = Session('codex-one', '保留中文标题', '/project', [
            Message('user', '实现导入', timestamp='2026-11-16T01:00:00Z'),
            Message('assistant', '已经实现'),
            Message('assistant', '{"cmd":"touch dangerous"}', kind='tool_call', name='exec_command'),
            Message('tool', '历史执行结果', kind='tool_result', call_id='call-1'),
            Message('system', '压缩后当前进度', kind='summary'),
            Message('user', '继续完善'),
        ], created_at='2026-11-16T00:00:00Z')

    def test_native_history_preserves_text_without_replaying_tools(self):
        plan = DeepSeekTarget().plan(Bundle('codex', sessions=[self.session]), self.options)
        artifact, rows = session_rows(plan)
        self.assertIn('/' + project_key('/project') + '/', artifact.path)
        self.assertEqual(rows[0]['version'], 4)
        self.assertEqual(rows[0]['cwd'], '/project')
        self.assertFalse(rows[0]['isSeeded'])
        self.assertEqual(rows[0]['delegationDepth'], 0)
        events = rows[1:]
        self.assertEqual([event['seq'] for event in events], list(range(len(events))))
        self.assertFalse(any(event['type'] in ('tool/call', 'tool/result') for event in events))
        assistant = next(event for event in events if event['type'] == 'assistant/message')
        self.assertEqual(assistant['data']['message']['content'][0]['text'], '已经实现')
        self.assertEqual(assistant['data']['stream'][0]['texts'], ['已经实现'])
        self.assertEqual(assistant['data']['stream'][0]['dt'], [])
        users = [event['data'] for event in events if event['type'] == 'user/message']
        self.assertEqual([message['content'][0]['text'] for message in users if message['source']['kind'] == 'user'], ['实现导入', '继续完善'])
        recalls = '\n'.join(message['content'][0]['text'] for message in users)
        self.assertIn('touch dangerous', recalls)
        self.assertIn('历史执行结果', recalls)
        self.assertIn('压缩后当前进度', recalls)
        self.assertEqual(events[-1]['type'], 'session/title')
        self.assertEqual(events[-1]['data']['title'], '保留中文标题')
        self.assertFalse(self.home.exists(), 'Planning must not create directories')

    def test_repeat_plan_is_byte_identical(self):
        bundle = Bundle('codex', sessions=[self.session])
        one = DeepSeekTarget().plan(bundle, self.options)
        two = DeepSeekTarget().plan(bundle, self.options)
        self.assertEqual(one.session_map, two.session_map)
        self.assertEqual(one.files, two.files)

    def test_target_supports_other_source_adapters_without_id_collision(self):
        codex = DeepSeekTarget().plan(Bundle('codex', sessions=[self.session]), self.options)
        other = DeepSeekTarget().plan(Bundle('other-agent', sessions=[self.session], documents=[Document('AGENTS.md', 'rules')]), self.options)
        self.assertNotEqual(codex.session_map, other.session_map)
        self.assertTrue(any(file.path.startswith('imports/other-agent/') for file in other.files))

    def test_skills_binary_and_scoped_project_docs(self):
        docs = [Document('AGENTS.md', '使用中文', kind='instruction'),
                Document('skills/demo/SKILL.md', '技能说明', kind='skill'),
                Document('skills/demo/icon.bin', base64.b64encode(b'\x00\xff').decode(), kind='skill', encoding='base64'),
                Document('AGENTS.md', '仅项目规则', scope='project', project='/project'),
                Document('AGENTS.md', '其他项目规则', scope='project', project='/other'),
                Document('memories/notes.md', '用户偏好', kind='memory')]
        plan = DeepSeekTarget().plan(Bundle('codex', sessions=[self.session], documents=docs), self.options)
        files = {file.path: file.content for file in plan.files}
        self.assertEqual(files['skills/demo/icon.bin'], b'\x00\xff')
        self.assertEqual(files['skills/demo/SKILL.md'], '技能说明'.encode())
        self.assertIn('使用中文', files['AGENTS.md'].decode())
        self.assertIn('memories/notes.md', files['AGENTS.md'].decode())
        self.assertIn(str(self.home / 'imports/codex/documents/global/memories/notes.md'), files['AGENTS.md'].decode())
        _, rows = session_rows(plan)
        context = next(row['data']['content'][0]['text'] for row in rows[1:] if row['type'] == 'user/message')
        self.assertIn('仅项目规则', context)
        self.assertNotIn('其他项目规则', context)

    def test_project_skill_target_inside_another_git_root_is_refused_before_writing(self):
        repository = Path(self.temp.name) / 'repository'
        (repository / '.git').mkdir(parents=True)
        child = repository / 'package'
        child.mkdir()
        document = Document('.codex/skills/review/SKILL.md', '---\nname: review\ndescription: Review code\n---\nReview instructions', kind='skill', scope='project', project=str(child))
        with self.assertRaisesRegex(ValueError, '原生项目根'):
            DeepSeekTarget().plan(Bundle('codex', documents=[document]), self.options)
        self.assertFalse((child / '.dsh').exists())

    def test_existing_global_instructions_preserved_and_managed_once(self):
        self.home.mkdir()
        original = '目标已有规则\n'
        (self.home / 'AGENTS.md').write_text(original)
        bundle = Bundle('codex', documents=[Document('AGENTS.md', '源规则')])
        one = DeepSeekTarget().plan(bundle, self.options)
        merged = next(file.content for file in one.files if file.path == 'AGENTS.md')
        (self.home / 'AGENTS.md').write_bytes(merged)
        two = DeepSeekTarget().plan(bundle, self.options)
        repeated = next(file.content for file in two.files if file.path == 'AGENTS.md')
        self.assertEqual(merged, repeated)
        self.assertTrue(merged.decode().startswith(original.rstrip()))
        self.assertEqual(merged.decode().count('源规则'), 1)

    def test_project_mapping_applies_to_subdirectories(self):
        self.session.cwd = '/project/nested'
        options = TargetOptions(self.home, project_map={'/project': '/new/location'})
        plan = DeepSeekTarget().plan(Bundle('codex', sessions=[self.session]), options)
        _, rows = session_rows(plan)
        self.assertEqual(rows[0]['cwd'], '/new/location/nested')

    def test_project_alias_mapping_and_instruction_directory_scope(self):
        real = Path(self.temp.name) / 'project'
        real.mkdir()
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        self.session.cwd = str(alias)
        docs = [Document('AGENTS.md', 'root normal', scope='project', project=str(real)),
                Document('AGENTS.override.md', 'root override', scope='project', project=str(real)),
                Document('nested/AGENTS.md', 'nested only', scope='project', project=str(real)),
                Document('.codex/skills/demo/script.py', 'do not inject skill code', kind='skill', scope='project', project=str(real))]
        options = TargetOptions(self.home, project_map={str(real): '/new/location'})
        plan = DeepSeekTarget().plan(Bundle('codex', sessions=[self.session], documents=docs), options)
        _, rows = session_rows(plan)
        self.assertEqual(rows[0]['cwd'], '/new/location')
        context = next(row['data']['content'][0]['text'] for row in rows[1:] if row['type'] == 'user/message')
        self.assertIn('root override', context)
        self.assertNotIn('root normal', context)
        self.assertNotIn('nested only', context)
        self.assertNotIn('do not inject skill code', context)
        self.assertIn('仅适用于 /new/location', context)
        native_rules = [file for file in plan.files if file.root.startswith('project:') and file.path == 'AGENTS.md']
        self.assertEqual(len(native_rules), 1)
        self.assertIn(b'root override', native_rules[0].content)
        self.assertNotIn(b'root normal', native_rules[0].content)

    def test_plaintext_backend_and_invalid_compression(self):
        options = TargetOptions(self.home, compression='none')
        plan = DeepSeekTarget().plan(Bundle('codex', sessions=[self.session]), options)
        artifact = next(file for file in plan.files if file.path.endswith('.jsonl'))
        rows = [json.loads(line) for line in artifact.content.splitlines()]
        self.assertEqual(rows[0]['version'], 4)
        self.assertEqual(rows[-1]['type'], 'session/title')
        with self.assertRaises(ValueError):
            DeepSeekTarget().plan(Bundle('codex'), TargetOptions(self.home, compression='invalid'))

    def project_files(self, plan):
        return {(str(plan.project_roots[file.root]), file.path): file
                for file in plan.files if file.root.startswith('project:')}

    def test_project_rules_for_new_sessions_preserve_target_and_nested_scope(self):
        source = Path(self.temp.name) / 'source-project'
        target = Path(self.temp.name) / 'target-project'
        target.mkdir()
        (target / 'AGENTS.md').write_text('existing target rule\n')
        docs = [Document('AGENTS.md', 'ignored source base', scope='project', project=str(source)),
                Document('AGENTS.override.md', 'effective override', scope='project', project=str(source)),
                Document('child/AGENTS.md', 'child only', scope='project', project=str(source)),
                Document('codex-config-developer_instructions.md', 'config directions', scope='project', project=str(source))]
        options = TargetOptions(self.home, project_map={str(source): str(target)})
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), options)
        files = self.project_files(plan)
        root = files[(str(target), 'AGENTS.md')]
        self.assertTrue(root.merge)
        self.assertIn(b'existing target rule', root.content)
        self.assertIn(b'effective override', root.content)
        self.assertIn(b'config directions', root.content)
        self.assertNotIn(b'ignored source base', root.content)
        self.assertNotIn(b'child only', root.content)
        self.assertEqual(files[(str(target), 'child/AGENTS.md')].content, b'child only')
        self.assertFalse((target / 'child').exists(), 'Planning must not write project files')

    def test_inplace_override_retires_only_exact_ignored_source_base(self):
        project = Path(self.temp.name) / 'project'
        project.mkdir()
        (project / 'AGENTS.md').write_text('ignored source base\n')
        docs = [Document('AGENTS.md', 'ignored source base\n', scope='project', project=str(project)),
                Document('AGENTS.override.md', 'effective override', scope='project', project=str(project))]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        rules = self.project_files(plan)[(str(project), 'AGENTS.md')]
        self.assertNotIn(b'ignored source base', rules.content)
        self.assertIn(b'effective override', rules.content)
        self.assertTrue(rules.merge)
        (project / 'AGENTS.md').write_bytes(rules.content)
        repeated = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        self.assertEqual(self.project_files(repeated)[(str(project), 'AGENTS.md')].content, rules.content)

    def test_inplace_base_kept_outside_managed_region_with_config_added_once(self):
        project = Path(self.temp.name) / 'project'
        project.mkdir()
        original = 'base project rule\n'
        (project / 'AGENTS.md').write_text(original)
        docs = [Document('AGENTS.md', original, scope='project', project=str(project)),
                Document('codex-config-developer_instructions.md', 'custom config rule', scope='project', project=str(project))]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        content = self.project_files(plan)[(str(project), 'AGENTS.md')].content
        self.assertTrue(content.startswith(original.encode()))
        self.assertEqual(content.count(b'base project rule'), 1)
        (project / 'AGENTS.md').write_bytes(content)
        docs[0].content = content.decode()
        again = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        repeated = self.project_files(again)[(str(project), 'AGENTS.md')].content
        self.assertEqual(repeated, content)
        self.assertEqual(repeated.count(b'custom config rule'), 1)

    def test_project_skill_native_bundle_preserves_assets_modes_and_invocation(self):
        project = str(Path(self.temp.name) / 'project')
        skill = '---\nname: ReviewHelper\ndescription: >\n  Review project changes\n  carefully.\nmodelInvocable: false\nuserInvocable: true\nmetadata:\n  author: "example"\n---\nRun scripts/check.sh, then inspect assets/blob.bin.\n'
        docs = [Document('.codex/skills/review/SKILL.md', skill, kind='skill', scope='project', project=project),
                Document('.codex/skills/review/scripts/check.sh', '#!/bin/sh\nprintf checked\n', kind='skill', scope='project', project=project, executable=True),
                Document('.codex/skills/review/assets/blob.bin', base64.b64encode(b'\x00\xff').decode(), kind='skill', scope='project', project=project, encoding='base64')]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        files = self.project_files(plan)
        text = files[(project, '.dsh/skills/review/SKILL.md')].content.decode()
        self.assertIn('name: "review-helper"', text)
        self.assertIn('description: "Review project changes carefully."', text)
        self.assertIn('disable-model-invocation: true', text)
        self.assertIn('user-invocable: true', text)
        self.assertNotIn('modelInvocable:', text)
        self.assertIn('metadata:\n  author: "example"', text)
        self.assertIn('Run scripts/check.sh', text)
        self.assertEqual(files[(project, '.dsh/skills/review/scripts/check.sh')].mode, 0o700)
        self.assertEqual(files[(project, '.dsh/skills/review/assets/blob.bin')].content, b'\x00\xff')
        self.assertEqual(files[(project, '.dsh/skills/review/assets/blob.bin')].mode, 0o600)

    def test_project_skill_conflicting_names_remain_separately_discoverable(self):
        project = str(Path(self.temp.name) / 'project')
        one = '---\nname: helper\ndescription: First\n---\nfirst body\n'
        two = '---\nname: helper\ndescription: Second\n---\nsecond body\n'
        docs = [Document('.codex/skills/one/SKILL.md', one, kind='skill', scope='project', project=project),
                Document('.agents/skills/two/SKILL.md', two, kind='skill', scope='project', project=project)]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        files = self.project_files(plan)
        self.assertIn(b'name: "helper"', files[(project, '.dsh/skills/one/SKILL.md')].content)
        self.assertIn(b'name: "agents-helper"', files[(project, '.dsh/skills/two/SKILL.md')].content)
        self.assertTrue(any('agents-helper' in warning for warning in plan.warnings))

    def test_identical_project_skills_deduplicate_as_whole_tree(self):
        project = str(Path(self.temp.name) / 'project')
        text = '---\nname: helper\ndescription: Help\n---\nbody\n'
        docs = [Document(prefix + '/helper/SKILL.md', text, kind='skill', scope='project', project=project)
                for prefix in ('.codex/skills', '.agents/skills')]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        files = self.project_files(plan)
        self.assertEqual(list(files), [(project, '.dsh/skills/helper/SKILL.md')])

    def test_nested_source_skill_is_flattened_into_native_discovery_root(self):
        project = str(Path(self.temp.name) / 'project')
        text = '---\nname: nested-helper\ndescription: Help with nested tasks\n---\nRead assets/input.bin and run scripts/check.sh.\n'
        docs = [Document('.codex/skills/group/nested/SKILL.md', text, kind='skill', scope='project', project=project),
                Document('.codex/skills/group/nested/assets/input.bin', base64.b64encode(b'\x00\xff').decode(), kind='skill', scope='project', project=project, encoding='base64'),
                Document('.codex/skills/group/nested/scripts/check.sh', '#!/bin/sh\nprintf nested\n', kind='skill', scope='project', project=project, executable=True)]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        files = self.project_files(plan)
        self.assertIn((project, '.dsh/skills/group-nested/SKILL.md'), files)
        self.assertNotIn((project, '.dsh/skills/group/nested/SKILL.md'), files)
        self.assertEqual(files[(project, '.dsh/skills/group-nested/assets/input.bin')].content, b'\x00\xff')
        self.assertEqual(files[(project, '.dsh/skills/group-nested/scripts/check.sh')].mode, 0o700)

    def test_empty_override_does_not_suppress_base_rule(self):
        project = str(Path(self.temp.name) / 'project')
        docs = [Document('AGENTS.md', 'effective base', scope='project', project=project),
                Document('AGENTS.override.md', ' \n', scope='project', project=project)]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        self.assertEqual(self.project_files(plan)[(project, 'AGENTS.md')].content, b'effective base')

    def test_project_skill_invalid_invocation_fails_instead_of_invisible_import(self):
        project = str(Path(self.temp.name) / 'project')
        text = '---\nname: helper\ndescription: Help\ndisable-model-invocation: sometimes\n---\nbody\n'
        with self.assertRaisesRegex(ValueError, 'invocation'):
            DeepSeekTarget().plan(Bundle('codex', documents=[Document('.codex/skills/helper/SKILL.md', text, kind='skill', scope='project', project=project)]), self.options)

    def test_project_archives_are_immutable_snapshots_when_source_changes(self):
        project = str(Path(self.temp.name) / 'project')
        doc = Document('AGENTS.md', 'first version', scope='project', project=project)
        one = DeepSeekTarget().plan(Bundle('codex', documents=[doc]), self.options)
        doc.content = 'second version'
        two = DeepSeekTarget().plan(Bundle('codex', documents=[doc]), self.options)
        first = next(file.path for file in one.files if file.path.startswith('imports/') and file.path.endswith('/AGENTS.md'))
        second = next(file.path for file in two.files if file.path.startswith('imports/') and file.path.endswith('/AGENTS.md'))
        self.assertNotEqual(first, second)

    def test_project_instruction_budget_is_reported_and_oversize_file_refused(self):
        project = str(Path(self.temp.name) / 'project')
        docs = [Document('AGENTS.md', 'a' * 40000, scope='project', project=project),
                Document('nested/AGENTS.md', 'b' * 40000, scope='project', project=project)]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        self.assertTrue(any('规则链' in warning for warning in plan.warnings))
        docs[0].content = 'a' * (1_048_576 + 1)
        with self.assertRaisesRegex(ValueError, '1 MiB'):
            DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)

    def test_same_skill_from_shared_and_global_deduplicates(self):
        docs = [Document('skills/demo/SKILL.md', 'same', kind='skill'),
                Document('skills/demo/SKILL.md', 'same', kind='skill', scope='shared')]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        skills = [file for file in plan.files if file.path == 'skills/demo/SKILL.md']
        self.assertEqual(len(skills), 1)

    def test_conflicting_skill_trees_are_preserved_without_mixing_files(self):
        docs = [Document('skills/demo/SKILL.md', 'global', kind='skill'),
                Document('skills/demo/helper.py', 'global helper', kind='skill'),
                Document('skills/demo/SKILL.md', 'shared', kind='skill', scope='shared'),
                Document('skills/demo/only-shared.py', 'shared helper', kind='skill', scope='shared')]
        plan = DeepSeekTarget().plan(Bundle('codex', documents=docs), self.options)
        files = {file.path: file.content for file in plan.files}
        self.assertEqual(files['skills/demo/SKILL.md'], b'global')
        self.assertNotIn('skills/demo/only-shared.py', files)
        self.assertEqual(files['imports/codex/documents/shared/skills/demo/SKILL.md'], b'shared')
        self.assertEqual(files['imports/codex/documents/shared/skills/demo/only-shared.py'], b'shared helper')
        self.assertTrue(any('同名技能 demo' in warning for warning in plan.warnings))

    def test_existing_opposite_session_encoding_refuses_compression_switch(self):
        bundle = Bundle('codex', sessions=[self.session])
        plan = DeepSeekTarget().plan(bundle, self.options)
        artifact = next(file for file in plan.files if file.path.endswith('.zstd'))
        destination = self.home / artifact.path
        destination.parent.mkdir(parents=True)
        destination.write_bytes(artifact.content)
        with self.assertRaisesRegex(ValueError, '禁止在同一会话目录混用'):
            DeepSeekTarget().plan(bundle, TargetOptions(self.home, compression='none'))
        self.assertFalse(destination.with_suffix('').exists())
        # The original matching encoding remains idempotent.
        repeated = DeepSeekTarget().plan(bundle, self.options)
        self.assertEqual(repeated.files, plan.files)
        destination.with_suffix('').write_bytes(b'plaintext')
        with self.assertRaisesRegex(ValueError, 'compression'):
            DeepSeekTarget().plan(bundle, self.options)

    def test_rejects_document_traversal_and_symlink_agents(self):
        for path in ('../escape', '/absolute', 'skills/../../outside', 'bad\\file'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                DeepSeekTarget().plan(Bundle('codex', documents=[Document(path, 'x')]), self.options)
        self.home.mkdir()
        (self.home / 'AGENTS.md').symlink_to(Path(self.temp.name) / 'outside')
        with self.assertRaises(ValueError):
            DeepSeekTarget().plan(Bundle('codex', documents=[Document('AGENTS.md', 'x')]), self.options)

    def test_native_directory_encoding_vectors(self):
        self.assertEqual(project_key('/tmp/.real-migration/projects/pet'), '--tmp-.real-migration-projects-pet--')
        self.assertEqual(project_key('/tmp/a.b/.hidden'), '--tmp-a.b-.hidden--')
        self.assertEqual(project_key('.'), '--.--')
        self.assertEqual(project_key('..'), '--..--')
        self.assertEqual(project_key('/tmp/./../file'), '--tmp-.-..-file--')
        self.assertEqual(project_key('/tmp/~'), '--tmp-~007E--')
        self.assertEqual(project_key('/中/😀'), '--~4E2D-~D83D~DE00--')
        self.assertEqual(project_key('/' + 'x' * 300), '--' + 'x' * 251 + '--')
        self.assertEqual(project_key('/Users/a/work'), '--Users-a-work--')
        self.assertEqual(project_key('/'), '--root--')
        self.assertEqual(project_key('C:\\work\\test'), '--C-work-test--')
        self.assertEqual(encode_segment('../x'), '..~002Fx')
        self.assertEqual(encode_segment('..'), '~002E~002E')
        self.assertEqual(encode_segment('中😀'), '~4E2D~D83D~DE00')

    @unittest.skipUnless(os.environ.get('MIGRATE_AGENT_DSH_NODE_MODULES') and shutil.which('node'),
                         'Set MIGRATE_AGENT_DSH_NODE_MODULES to compare native path helpers')
    def test_path_encoding_matches_installed_native_helpers(self):
        vectors = ['.', '..', '/', '/tmp/.real-migration/projects/pet', '/tmp/a.b/.hidden',
                   '/tmp/./../file', '/中/😀', '/tmp/~name', 'C:' + chr(92) + 'work' + chr(92) + '.hidden',
                   '///root::' + chr(92) + 'file', '/' + 'x' * 300, '/tmp/\ud800']
        script = r'''
import fs from 'node:fs';
import path from 'node:path';
import {createRequire, registerHooks, stripTypeScriptTypes} from 'node:module';
import {pathToFileURL} from 'node:url';
const root=path.resolve(process.env.MIGRATE_AGENT_DSH_NODE_MODULES);
const prefix=pathToFileURL(root+path.sep).href;
const resolver=createRequire(path.join(root,'..','migrate-agent-path-check.mjs'));
registerHooks({load(url,context,next){
  if(url.startsWith(prefix)&&url.endsWith('.ts'))return {
    format:'module',source:stripTypeScriptTypes(fs.readFileSync(new URL(url),'utf8'),{mode:'transform',sourceUrl:url}),shortCircuit:true
  };
  return next(url,context);
}});
const native=await import(pathToFileURL(resolver.resolve('@deepseek-ai/dsh-session-persistence-jsonl/src/format.ts')).href);
const vectors=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(vectors.map(value=>({project:native.projectKey(value),segment:native.encodeSegment(value)}))));
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script],
                                input=json.dumps(vectors).encode('ascii'),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        native = json.loads(result.stdout)
        expected = [{'project': project_key(value), 'segment': encode_segment(value)} for value in vectors]
        self.assertEqual(native, expected)

    def test_raw_frames_have_valid_boundary_sizes(self):
        for size in (0, 1, 255, 256, 65791, 65792, 131072, 131073, 300000):
            with self.subTest(size=size):
                data = b'x' * size
                self.assertEqual(decode_raw_frames(zstd_raw_frame(data)), [data])

    @unittest.skipUnless(shutil.which('zstd'), 'Optional independent Zstandard decoder')
    def test_standard_zstd_decoder_reads_native_container(self):
        plan = DeepSeekTarget().plan(Bundle('codex', sessions=[self.session]), self.options)
        artifact = next(file for file in plan.files if file.path.endswith('.zstd'))
        result = subprocess.run(['zstd', '-d', '-c', '-q'], input=artifact.content,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]['id'], plan.session_map[self.session.id])
        self.assertEqual(rows[-1]['data']['title'], self.session.title)
