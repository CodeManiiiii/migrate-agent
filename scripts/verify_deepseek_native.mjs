// Optional integration check against an installed Harness distribution.
// Run with Node supporting Zstandard and --experimental-transform-types.
// No model call and no writes: stdout contains counts, never message bodies.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire, registerHooks, stripTypeScriptTypes } from 'node:module';
import { pathToFileURL } from 'node:url';
import { zstdDecompressSync } from 'node:zlib';

const [nodeModules, artifactPath] = process.argv.slice(2);
if (!nodeModules || !artifactPath) {
  throw new Error('Usage: node --experimental-transform-types scripts/verify_deepseek_native.mjs /path/to/harness/node_modules /path/to/session.v4.jsonl[.zstd]');
}
const resolver = createRequire(path.join(path.resolve(nodeModules), '..', 'migrate-agent-native-check.mjs'));
// Node normally refuses TypeScript below node_modules. Transpile the installed
// native source in memory, preserving relative imports and leaving files intact.
const nativeRootUrl = pathToFileURL(path.resolve(nodeModules) + path.sep).href;
registerHooks({
  load(url, context, nextLoad) {
    if (url.startsWith(nativeRootUrl) && url.endsWith('.ts')) {
      const source = stripTypeScriptTypes(fs.readFileSync(new URL(url), 'utf8'), { mode: 'transform', sourceUrl: url });
      return { format: 'module', source, shortCircuit: true };
    }
    return nextLoad(url, context);
  },
});
const load = async (name) => import(pathToFileURL(resolver.resolve(name)).href);
const { sessionFormatCatalog } = await load('@deepseek-ai/dsh-session-format-catalog');
const { Session, SessionId, SessionLogOffset } = await load('@deepseek-ai/dsh-session');
const { expandAssistantStream } = await load('@deepseek-ai/dsh-llm');
const { scanZstdFrames } = await load('@deepseek-ai/dsh-session-persistence-jsonl/src/zstd.ts');
const { scanLog } = await load('@deepseek-ai/dsh-session-persistence-jsonl/src/format.ts');

const bytes = fs.readFileSync(artifactPath);
let plaintext = bytes;
let frames = 0;
if (artifactPath.endsWith('.zstd')) {
  const scan = scanZstdFrames(bytes);
  assert.equal(scan.tornStart, undefined, 'Truncated Zstandard frame');
  frames = scan.frames.length;
  assert.ok(frames >= 2, 'Header and events require independent frames');
  plaintext = Buffer.concat(scan.frames.map(({ start, end }) => zstdDecompressSync(bytes.subarray(start, end))));
}
const scanned = scanLog(plaintext);
const [header, ...events] = plaintext.toString('utf8').trimEnd().split('\n').map(JSON.parse);
const restore = sessionFormatCatalog.createRestore(header, { recovery: 'strict', validation: 'current' });
for (const event of events) restore.decodeRow(event);
const artifact = restore.finish();
assert.equal(artifact.events.length, events.length);
assert.equal(scanned.events.length, events.length);
const session = Session.fromRestore(
  SessionId(artifact.header.id), artifact.events, artifact.header,
  SessionLogOffset(artifact.inheritedEventCount), 'detached',
);
const messages = session.deriveMessages();
let assistantChunks = 0;
for (const event of artifact.events) {
  assert.notEqual(event.type, 'tool/call', 'Historical tools must not be executable');
  if (event.type === 'assistant/message') assistantChunks += expandAssistantStream(event.data.stream).length;
}
console.log(JSON.stringify({ status: 'verified', format: header.version, frames, events: events.length, messages: messages.length, assistantChunks }));
