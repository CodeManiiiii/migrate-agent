// Optional, read-only integration check against an installed Harness runtime.
// The instruction discovery/rendering and skill registry/provider are native
// Harness implementations. Every check uses a newly created, empty Session;
// no imported transcript, model API, or real user home is needed.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire, registerHooks, stripTypeScriptTypes } from 'node:module';
import { pathToFileURL } from 'node:url';

const [nodeModulesArgument, scenarioPath] = process.argv.slice(2);
if (!nodeModulesArgument || !scenarioPath) {
  throw new Error('Usage: node --experimental-transform-types scripts/verify_deepseek_project.mjs /path/to/harness/node_modules /path/to/scenario.json');
}
const nodeModules = path.resolve(nodeModulesArgument);
const resolver = createRequire(path.join(nodeModules, '..', 'migrate-agent-project-check.mjs'));
const nativeRootUrl = pathToFileURL(nodeModules + path.sep).href;
registerHooks({
  load(url, context, nextLoad) {
    if (url.startsWith(nativeRootUrl) && url.endsWith('.ts')) {
      const source = stripTypeScriptTypes(fs.readFileSync(new URL(url), 'utf8'), { mode: 'transform', sourceUrl: url });
      return { format: 'module', source, shortCircuit: true };
    }
    return nextLoad(url, context);
  },
});
const load = async name => import(pathToFileURL(resolver.resolve(name)).href);
const { Context } = await load('@deepseek-ai/cordis');
const { Session, SessionId } = await load('@deepseek-ai/dsh-session');
const { SkillRegistry, renderSkillContent } = await load('@deepseek-ai/dsh-skill');
const skillFilesystem = await load('@deepseek-ai/dsh-skill-filesystem');
const skillToolPlugin = await load('@deepseek-ai/dsh-tool-skill');
const { loadBaselineInstructionSet } = await load('@deepseek-ai/dsh-agent-instructions/src/files.ts');
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));
assert.ok(path.isAbsolute(scenario.dshHome), 'Scenario must isolate DSH_HOME');
assert.ok(path.isAbsolute(scenario.agentsHome), 'Scenario must isolate the shared agents home');
assert.ok(Array.isArray(scenario.cases) && scenario.cases.length > 0, 'At least one cwd case is required');

let freshSessions = 0;
let ruleFiles = 0;
let skillLoads = 0;
let toolLoads = 0;
let resourceChecks = 0;
for (const [index, check] of scenario.cases.entries()) {
  assert.ok(path.isAbsolute(check.cwd), 'Scenario cwd must be absolute');
  // New Session and new provider registry for every case avoid persisted
  // context, provider caches, or a previous session masking failed discovery.
  const session = Session.create(SessionId(`migrate-agent-project-check-${index}`));
  assert.equal(session.seq, 0, 'Verification session must start empty');
  assert.equal(session.deriveMessages().length, 0, 'No migrated history may provide these rules');
  freshSessions += 1;
  const baseline = await loadBaselineInstructionSet({
    cwd: check.cwd,
    dshHome: scenario.dshHome,
    agentsHome: scenario.agentsHome,
    maxBytes: scenario.maxBytes ?? 65536,
    maxSourceBytes: scenario.maxSourceBytes ?? 1048576,
  });
  const text = baseline?.rendered.text ?? '';
  for (const expected of check.rulesInclude ?? []) {
    assert.ok(text.includes(expected), `Expected active workspace rule missing in case ${index}`);
  }
  for (const forbidden of check.rulesExclude ?? []) {
    assert.ok(!text.includes(forbidden), `Unrelated or superseded rule active in case ${index}`);
  }
  let previousPosition = -1;
  for (const expected of check.rulesOrder ?? []) {
    const position = text.indexOf(expected);
    assert.ok(position > previousPosition, `Workspace precedence order differs in case ${index}`);
    previousPosition = position;
  }
  const includedPaths = new Set((baseline?.included ?? []).map(file => path.resolve(file.absolutePath)));
  for (const file of check.ruleFiles ?? []) {
    assert.ok(includedPaths.has(path.resolve(file)), `Native loader omitted expected instruction file in case ${index}`);
  }
  ruleFiles += includedPaths.size;

  const context = new Context();
  try {
    await context.plugin(SkillRegistry);
    await context.plugin(skillFilesystem, {
      dshHome: scenario.dshHome,
      agentsHome: scenario.agentsHome,
      // Override any bundled-skill directory inherited from the developer's
      // app environment, keeping this check entirely within its fixture roots.
      bundledSkillDir: path.join(scenario.dshHome, '.native-check-empty-bundled'),
      watch: false,
    });
    // Capture the real native skill tool definition. The tiny command host
    // supplies only registration and this fixture's working directory; skill
    // lookup, policy checks, loading, and output rendering remain native.
    const tools = [];
    const toolHost = context.extend({
      tools: { register: tool => tools.push(tool) },
      workingDirectory: { ensure: async () => check.cwd },
    });
    skillToolPlugin.apply(toolHost);
    const skillTool = tools.find(tool => tool.name === 'skill');
    assert.ok(skillTool, 'Native skill tool did not register');
    const execution = { agent: { session }, signal: new AbortController().signal };
    const snapshot = await context.skills.snapshot({ cwd: check.cwd });
    assert.equal(snapshot.complete, true, `Native skill discovery incomplete in case ${index}`);
    for (const absent of check.skillsAbsent ?? []) {
      assert.ok(!snapshot.skills.some(skill => skill.name === absent), `Unrelated skill leaked into case ${index}`);
    }
    for (const expected of check.skills ?? []) {
      const summary = snapshot.skills.find(skill => skill.name === expected.name);
      assert.ok(summary, `Expected project skill missing in case ${index}`);
      assert.equal(summary.source, expected.source ?? 'project-dsh');
      if (expected.modelInvocable !== undefined) assert.equal(summary.invocation.modelInvocable, expected.modelInvocable);
      if (expected.userInvocable !== undefined) assert.equal(summary.invocation.userInvocable, expected.userInvocable);
      const definition = await context.skills.get(expected.name, { cwd: check.cwd });
      assert.ok(definition, `Native skill body could not load in case ${index}`);
      assert.equal(definition.resourceBase?.kind, 'directory');
      if (expected.resourceBase) assert.equal(path.resolve(definition.resourceBase.path), path.resolve(expected.resourceBase));
      // This is the exact renderer used by the native skill tool output and
      // user-explicit /skill invocation, including relative-resource hints.
      const rendered = renderSkillContent(definition);
      assert.ok(rendered.includes(`<skill_content name="${expected.name}">`));
      assert.ok(rendered.includes(definition.resourceBase.path));
      for (const body of expected.contentInclude ?? []) {
        assert.ok(definition.content.includes(body), `Loaded skill instructions differ in case ${index}`);
        assert.ok(rendered.includes(body), `Model-facing skill instructions differ in case ${index}`);
      }
      if (definition.invocation.modelInvocable) {
        const loadedByTool = await skillTool.execute({ name: expected.name }, execution);
        assert.equal(loadedByTool.content, definition.content, `Native skill tool body differs in case ${index}`);
        const output = skillTool.output.render({ name: expected.name }, loadedByTool);
        assert.equal(output[0].text, rendered, `Native model-facing skill tool output differs in case ${index}`);
        toolLoads += 1;
      } else {
        await assert.rejects(skillTool.execute({ name: expected.name }, execution), /not available for model invocation/);
      }
      for (const resource of expected.resources ?? []) {
        assert.ok(!path.isAbsolute(resource.path) && !resource.path.split(/[\\/]/).includes('..'), 'Resource check must stay inside its skill');
        const bytes = fs.readFileSync(path.join(definition.resourceBase.path, resource.path));
        if (resource.sha256) assert.equal(createHash('sha256').update(bytes).digest('hex'), resource.sha256);
        if (resource.text !== undefined) assert.equal(bytes.toString('utf8'), resource.text);
        if (resource.executable === true) fs.accessSync(path.join(definition.resourceBase.path, resource.path), fs.constants.X_OK);
        resourceChecks += 1;
      }
      skillLoads += 1;
    }
  } finally {
    await context.fiber.dispose();
  }
}
console.log(JSON.stringify({ status: 'verified', freshSessions, ruleFiles, skillLoads, toolLoads, resourceChecks }));
