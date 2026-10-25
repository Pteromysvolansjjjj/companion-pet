import assert from 'node:assert/strict';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { mountCompanion } from '../../src/host.mjs';
import { registerChatProjection, CHAT_PROJECTION_KEY } from '../../src/chat-projection.mjs';
import { classifyGameWindow } from '../../src/client/chat-filter.mjs';

const flag = process.argv.indexOf('--app-dir');
if (flag < 0 || !process.argv[flag + 1]) throw new Error('Pass --app-dir with the installed DSH application directory.');
const appDir = resolve(process.argv[flag + 1]);
const sdkRoot = join(appDir, 'resources', 'app.asar', 'dsh', 'node_modules', '@deepseek-ai');
const sdk = name => import(pathToFileURL(join(sdkRoot, name, 'lib', 'index.js')).href);
const { Context } = await sdk('cordis');
const { default: LlmRuntime, LlmAdapter, createUserMessage } = await sdk('dsh-llm');
const { default: SessionStore } = await sdk('dsh-session');
const { default: Projections } = await sdk('dsh-session-projection');
const { z } = createRequire(join(sdkRoot, 'dsh-session-projection', 'package.json'))('zod');
const { default: SystemPrompt } = await sdk('dsh-system-prompt');
const { default: Tools, defineTool } = await sdk('dsh-tools');
const { default: Agents } = await sdk('dsh-agent');
const { default: AgentLoop } = await sdk('dsh-agent-loop');
const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const root = new Context();
const handles = [];
const displays = [];
const watchers = [];
const toolOutcomes = [];
const modelInputs = [];
const sessionEvents = new Map();
let desktopChatSubmit;
let modelPause;
const longReply = '字'.repeat(81) + '👩🏽‍💻' + 'e\u0301';
const longPages = ['字'.repeat(20), '字'.repeat(20), '字'.repeat(20), '字'.repeat(20), '字👩🏽‍💻e\u0301'];
let reads = 0;
let closeCount = 0;
let cleanup;
let calls = 0;
let now = 0, timerId = 0;
const timers = new Map();
const clock = { now: () => now, setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, at: now + ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
async function tick(milliseconds = 2000) {
  now += milliseconds;
  for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); }
  for (let i = 0; i < 20; i++) await Promise.resolve();
}
const deadline = setTimeout(() => { console.error('SDK integration exceeded 20 seconds'); process.exit(2); }, 20_000);

class FixtureAdapter extends LlmAdapter {
  started = new Set();
  async *stream(options) {
    calls++;
    const input = options.messages.findLast(message => message.role === 'user');
    assert.ok(input);
    const text = input.content.filter(block => block.type === 'text').map(block => block.text).join('\n');
    modelInputs.push({ session: options.sessionId, text: JSON.stringify(options) });
    const commandText = text.startsWith('fixture:') ? text.slice(8) : null;
    const command = commandText?.split(' ', 1)[0];
    const tool = { start: 'lol_watch_start', stop: 'lol_watch_stop', read: 'lol_read', tune: 'lol_tune' }[command];
    const args = command === 'tune' && commandText.includes(' ') ? commandText.slice(commandText.indexOf(' ') + 1) : '{}';
    const first = !this.started.has(input.id);
    this.started.add(input.id);
    if (tool && first) {
      assert.ok(options.tools.some(definition => definition.name === tool));
      yield { type: 'block-start', index: 0, blockType: 'tool-call' };
      yield { type: 'block-end', index: 0, block: { type: 'tool-call', id: `fixture-call-${calls}`, name: tool, arguments: args } };
    } else {
      if (tool) {
        const result = options.messages.findLast(message => message.role === 'tool');
        assert.ok(result && !result.isError, JSON.stringify(result));
        const rendered = result.content.filter(block => block.type === 'text').map(block => block.text).join('');
        toolOutcomes.push({ session: options.sessionId, tool, value: JSON.parse(rendered) });
      }
      const automatic = input.source?.kind === 'lol-companion';
      if (automatic && text.includes('"fixtureMixed":true') && first) {
        root.agents.get(options.sessionId).steer(createUserMessage({
          content: [{ type: 'text', text: 'fixture:manual-long' }], source: { kind: 'user' },
        }));
      }
      if (automatic && text.includes('"fixtureDesktopHold":true') && first) {
        assert.ok(modelPause, 'The test must install its deterministic model gate');
        modelPause.entered.resolve();
        await modelPause.release.promise;
      }
      const reply = command === 'manual-long' || automatic && text.includes('"fixtureLong":true') ? longReply
        : text.includes('"fixtureSilent":true') ? '<silent/>'
        : `${text.startsWith('[LOL 实时数据]') ? 'GAME' : command?.toUpperCase() || 'CHAT'} ${options.sessionId}`;
      yield { type: 'block-start', index: 0, blockType: 'text' };
      yield { type: 'text-delta', index: 0, text: reply };
      yield { type: 'block-end', index: 0, block: { type: 'text', text: reply } };
    }
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}

function fixtureDesktop() {
  // No onPlayback/onPreferences: renderer callbacks are covered by separate host/protocol tests.
  return {
    start() {},
    status: () => ({ running: true, fixture: true }),
    send(message) { displays.push(structuredClone(message)); },
    onChatSubmit(handler) { desktopChatSubmit = handler; return () => { desktopChatSubmit = undefined; }; },
    close() { closeCount++; },
  };
}
function createWatcher(options) {
  const watcher = {
    running: false,
    stopped: false,
    intervalMs: options.intervalMs,
    dataMode: options.dataMode,
    status: () => ({ running: watcher.running, fixture: true, intervalMs: watcher.intervalMs, dataMode: watcher.dataMode }),
    setIntervalMs(value) { watcher.intervalMs = value; },
    setDataMode(value) { watcher.dataMode = value; },
    start() { watcher.running = true; },
    async stop() { watcher.running = false; watcher.stopped = true; },
    emit: update => options.onUpdate(update, { signal: new AbortController().signal }),
  };
  watchers.push(watcher);
  return watcher;
}
async function send(agent, text) {
  const message = createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'sdk-test' } });
  assert.equal(agent.followup(message), undefined);
  await agent.whenIdle();
}

async function submitDesktop(requestId, text) {
  assert.equal(typeof desktopChatSubmit, 'function');
  const bindingId = displays.findLast(message => message.type === 'preferences').bindingId;
  assert.ok(bindingId);
  const before = displays.length;
  await desktopChatSubmit({ type: 'chat:submit', bindingId, requestId, text });
  const result = displays.slice(before).findLast(message => message.type === 'chat:result' && message.requestId === requestId);
  assert.deepEqual(result, { type: 'chat:result', bindingId, requestId, accepted: true });
  return result;
}

function assertDesktopTextVisible(agent, text, { mixed = false } = {}) {
  const events = sessionEvents.get(agent.session.id) ?? [];
  const index = events.findLastIndex(event => event.type === 'user/message'
    && event.data.content?.some(block => block.type === 'text' && block.text === text));
  assert.ok(index >= 0, 'Original desktop text must be committed to the original session');
  const input = events[index];
  assert.equal(input.data.source.kind, 'user', 'Use the normal SDK human source, not an automatic/plugin source');
  const turnStart = events.slice(0, index).findLast(event => event.type === 'turn/start');
  assert.ok(turnStart);
  const turnEnd = events.findIndex((event, offset) => offset > index && event.type === 'turn/end');
  assert.ok(turnEnd > index);
  const replies = events.slice(index + 1, turnEnd).filter(event => event.type === 'assistant/message');
  assert.ok(replies.length > 0, 'The original conversation must contain its normal assistant response');
  const view = root.sessionProjections.stateOf(agent.session, CHAT_PROJECTION_KEY).view;
  assert.ok(!view.hiddenTurns.includes(turnStart.data.turn), 'Human and mixed turns must remain visible');
  if (mixed) assert.ok(view.mixedTurns.includes(turnStart.data.turn));
  const visibility = classifyGameWindow(events.map(event => ({ event })), view);
  assert.equal(visibility.get(input), false, 'The actual Chat classifier must keep the submitted user text');
  for (const reply of replies) assert.equal(visibility.get(reply), false, 'The actual Chat classifier must keep the assistant answer');
  return turnStart.data.turn;
}

try {
  for (const service of [LlmRuntime, SessionStore, Projections]) await root.plugin(service);
  registerChatProjection(root, z);
  await root.plugin(SystemPrompt, { includeHarnessIdentity: false, includeRuntimeContext: false });
  for (const service of [Tools, Agents, AgentLoop]) await root.plugin(service);
  root.on('session/event', (session, event) => {
    const events = sessionEvents.get(session.id) ?? [];
    events.push(event); sessionEvents.set(session.id, events);
  });
  root.llm.registerAdapter(['lol-sdk-fixture'], new FixtureAdapter());
  cleanup = mountCompanion(root, { defineTool, createUserMessage }, {}, {
    root: projectRoot, persistStatus: false, persistPreferences: false, desktop: fixtureDesktop(), createWatcher, clock,
    connector: { async read() { reads++; return { capturedAt: 'fixture', sections: {}, fixture: true }; } },
  });
  for (const sessionId of ['sdk-fixture-A', 'sdk-fixture-B']) {
    handles.push(await root.agents.create({ sessionId, agentOptions: { provider: 'lol-sdk-fixture', model: 'fixture' } }));
  }
  const [a, b] = handles.map(handle => handle.agent);
  await send(a, 'fixture:read');
  assert.equal(reads, 1);
  assert.equal(toolOutcomes.at(-1).value.snapshot.fixture, true);
  assert.equal(displays.filter(message => message.type === 'reply').length, 0);

  await send(a, 'fixture:start');
  assert.equal(watchers.length, 1);
  assert.equal(toolOutcomes.at(-1).value.boundSessionId, a.session.id);
  assert.ok(displays.some(message => message.type === 'reply' && message.lines[0] === `START ${a.id}`));
  await send(a, 'fixture:start');
  assert.equal(watchers.length, 1, 'Starting twice must not create another watcher');
  assert.equal(watchers[0].intervalMs, 1000);
  assert.equal(watchers[0].dataMode, 'basic');

  await send(a, 'fixture:tune {"pollIntervalMs":500,"decisionIntervalMs":2000,"bubbleHoldMs":800,"replyLength":60,"dataMode":"basic"}');
  assert.deepEqual(toolOutcomes.at(-1).value, {
    sessionId: a.session.id, active: true,
    preferences: { pollIntervalMs: 500, decisionIntervalMs: 2000, bubbleHoldMs: 800, replyLength: 60, dataMode: 'basic' },
    bubblePageCharacters: 20, replyLengthIsHardLimit: false,
  });
  assert.equal(watchers.length, 1, 'Tuning a live session must not replace its watcher');
  assert.equal(watchers[0].intervalMs, 500);
  assert.equal(watchers[0].dataMode, 'basic');
  assert.equal(displays.at(-1).holdMs, 800);
  assert.ok(modelInputs.at(-1).text.includes('尽量在60字左右'), 'The next model step receives the updated preference');
  assert.ok(modelInputs.at(-1).text.includes('当前所选数据档为basic'), 'The model receives the active data mode');
  assert.ok(modelInputs.at(-1).text.includes('不把旧事件逐条排队补播'), 'The contract asks for a fresh combined decision instead of queued event replies');
  assert.deepEqual(displays.findLast(message => message.type === 'preferences').values, toolOutcomes.at(-1).value.preferences,
    'The tool result and desktop settings receive the same saved preferences');
  await send(a, 'fixture:tune');
  assert.equal(toolOutcomes.at(-1).value.preferences.replyLength, 60, 'Empty arguments query without resetting settings');

  const beforeOtherTune = displays.length;
  await send(b, 'fixture:tune {"decisionIntervalMs":500,"bubbleHoldMs":900,"replyLength":0,"dataMode":"detailed"}');
  assert.equal(toolOutcomes.at(-1).value.active, false);
  assert.equal(toolOutcomes.at(-1).value.preferences.pollIntervalMs, 500, 'A 500 ms decision interval automatically speeds up slower sampling when pollIntervalMs is omitted');
  assert.equal(watchers[0].intervalMs, 500, 'An unbound session cannot retune the active watcher');
  assert.equal(watchers[0].dataMode, 'basic', 'An unbound session cannot switch the active data mode');
  assert.equal(displays.length, beforeOtherTune, 'Tuning another session must not reach the active desktop');

  await watchers[0].emit({ id: 'game-1', context: { fixtureEvent: 'player-killed' } });
  const callsBeforeBatch = calls;
  await a.whenIdle(); assert.equal(calls, callsBeforeBatch, 'Buffered changes do not wake the model immediately');
  await tick(1999);
  await a.whenIdle(); assert.equal(calls, callsBeforeBatch, 'The new batching interval is respected');
  await tick(1);
  await a.whenIdle();
  assert.equal(displays.at(-1).lines[0], `GAME ${a.id}`);
  assert.ok(a.session.deriveMessages().some(message => message.role === 'user' && message.source.kind === 'lol-companion'));
  assert.equal(root.sessionProjections.stateOf(a.session, CHAT_PROJECTION_KEY).view.hiddenTurns.length, 1);
  const beforeOther = displays.length;
  await send(b, 'fixture:chat');
  assert.equal(displays.length, beforeOther, 'An unbound conversation must never reach the desktop');

  await watchers[0].emit({ id: 'game-2', context: { fixtureSilent: true } });
  await tick();
  await a.whenIdle();
  assert.equal(displays.at(-1).type, 'clear');
  assert.ok(!displays.some(message => message.type === 'reply' && message.lines.includes('<silent/>')));

  const beforeLong = calls;
  await watchers[0].emit({ id: 'game-3', context: { fixtureLong: true } });
  await tick(); await a.whenIdle();
  assert.equal(calls - beforeLong, 1, 'A long automatic reply never requests a formatting model call');
  assert.deepEqual(displays.at(-1), { type: 'reply', lines: longPages, holdMs: 800 });
  assert.equal(displays.at(-1).lines.join(''), longReply, 'Pagination preserves every grapheme without a total-length cap');

  const beforeManualLong = calls;
  await send(a, 'fixture:manual-long');
  assert.equal(calls - beforeManualLong, 1, 'Manual long replies are retained without automatic correction');
  assert.deepEqual(displays.at(-1).lines, longPages, 'Manual final plain text also reaches the bubble');
  const beforeMixed = calls;
  await watchers[0].emit({ id: 'game-mixed', context: { fixtureMixed: true } });
  await tick(); await a.whenIdle();
  assert.equal(calls - beforeMixed, 2, 'A human steering input must not receive a third formatting step');
  const view = root.sessionProjections.stateOf(a.session, CHAT_PROJECTION_KEY).view;
  assert.equal(view.mixedTurns.length, 1);
  assert.ok(!view.hiddenTurns.includes(view.mixedTurns[0]));
  assert.ok(a.session.deriveMessages().some(message => message.role === 'assistant' && message.content.some(block => block.text === longReply)), 'Pagination and visibility do not erase original response records');
  assert.ok(!a.session.deriveMessages().some(message => message.role === 'user' && message.content.some(block => block.text?.startsWith('上一条未符合'))), 'No format-repair input is injected into original history');

  const desktopText = '  桌宠原文：这局少说两句。\n保留换行和空格 🐋  ';
  const beforeDesktopIdle = calls;
  const otherSessionMessages = b.session.deriveMessages().length;
  await submitDesktop('desktop-idle', desktopText);
  await a.whenIdle();
  assert.equal(calls - beforeDesktopIdle, 1, 'Desktop input uses one ordinary call through the existing agent');
  assertDesktopTextVisible(a, desktopText);
  assert.equal(b.session.deriveMessages().length, otherSessionMessages, 'Desktop submission cannot reach the other conversation');
  const idleMessages = a.session.deriveMessages().length;
  await submitDesktop('desktop-idle', desktopText);
  await a.whenIdle();
  assert.equal(calls - beforeDesktopIdle, 1, 'Duplicate accepted desktop request does not repeat a model call');
  assert.equal(a.session.deriveMessages().length, idleMessages, 'Duplicate request does not append another user message');

  modelPause = { entered: Promise.withResolvers(), release: Promise.withResolvers() };
  const beforeDesktopRunning = calls;
  await watchers[0].emit({ id: 'desktop-running-auto', context: { fixtureDesktopHold: true } });
  await tick();
  await modelPause.entered.promise;
  assert.equal(a.status, 'running');
  const runningText = '正在回答也能接收这条桌宠输入';
  await submitDesktop('desktop-running', runningText);
  assert.equal(a.status, 'running', 'Acceptance acknowledges enqueueing, not completion of the current model stream');
  assert.equal(calls - beforeDesktopRunning, 1, 'Desktop acceptance cannot interrupt or start an overlapping model request');
  modelPause.release.resolve();
  await a.whenIdle();
  modelPause = undefined;
  assert.equal(calls - beforeDesktopRunning, 2, 'Queued manual input runs as one ordinary subsequent turn');
  const runningManualTurn = assertDesktopTextVisible(a, runningText);
  const afterRunningView = root.sessionProjections.stateOf(a.session, CHAT_PROJECTION_KEY).view;
  assert.ok(!afterRunningView.mixedTurns.includes(runningManualTurn), 'followup while running starts a separate normal turn');

  // Real SDK claims all next-step context plus one next-turn message. Test that
  // explicit mixed admission remains visible; ordinary followups stay separate.
  const maintenanceEntered = Promise.withResolvers();
  const maintenanceRelease = Promise.withResolvers();
  const maintenance = a.runMaintenance(async () => {
    maintenanceEntered.resolve(); await maintenanceRelease.promise;
  });
  await maintenanceEntered.promise;
  const beforeDesktopMixed = calls;
  a.inject(createUserMessage({ content: [{ type: 'text', text: '[LOL 实时数据]\n{"fixtureEvent":"desktop-mixed-game-state"}' }], source: { kind: 'lol-companion' } }));
  const mixedText = '这句桌宠消息和游戏数据同轮到达';
  try {
    await submitDesktop('desktop-mixed', mixedText);
    assert.equal(calls, beforeDesktopMixed, 'Both inputs wait for the original agent admission boundary');
  } finally { maintenanceRelease.resolve(); }
  await maintenance;
  await a.whenIdle();
  assert.equal(calls - beforeDesktopMixed, 1, 'A mixed admission is handled by one existing-agent response');
  assertDesktopTextVisible(a, mixedText, { mixed: true });
  assert.ok(modelInputs.every(input => input.session === a.session.id || input.session === b.session.id), 'Desktop input never creates an extra model/session route');

  await send(b, 'fixture:start');
  assert.equal(watchers.length, 2);
  assert.equal(watchers[1].intervalMs, 500, 'Starting B restores B\'s automatically adjusted sampling interval');
  assert.equal(watchers[1].dataMode, 'detailed', 'Starting B restores B\'s own data mode');
  assert.equal(toolOutcomes.at(-1).value.preferences.replyLength, 0);
  assert.ok(modelInputs.at(-1).text.includes('回复总字数不限'), 'Zero removes the total-length preference from the dynamic prompt');
  assert.ok(modelInputs.at(-1).text.includes('当前所选数据档为detailed'));
  assert.equal(displays.at(-1).holdMs, 900);
  assert.equal(watchers[0].stopped, true);
  await assert.rejects(watchers[0].emit({ stale: true }), /LISTENER_STOPPED/);
  const beforeOld = displays.length;
  await send(a, 'fixture:chat');
  assert.equal(displays.length, beforeOld, 'Previously bound session must stop displaying replies');
  await send(a, 'fixture:stop');
  assert.equal(toolOutcomes.at(-1).value.stopped, false);
  assert.equal(toolOutcomes.at(-1).value.reason, 'LISTENER_BELONGS_TO_ANOTHER_SESSION');
  assert.equal(watchers[1].running, true);
  await watchers[1].emit({ id: 'game-5', context: { fixtureEvent: 'victory' } });
  const beforeFastBatch = calls;
  await tick(499); await b.whenIdle();
  assert.equal(calls, beforeFastBatch, 'The 500 ms interval must not deliver early');
  await tick(1);
  await b.whenIdle();
  assert.equal(displays.at(-1).lines[0], `GAME ${b.id}`);
  assert.equal(displays.at(-1).holdMs, 900);

  await send(b, 'fixture:stop');
  assert.equal(toolOutcomes.at(-1).value.stopped, true);
  assert.equal(watchers[1].stopped, true);
  await assert.rejects(watchers[1].emit({ stale: true }), /LISTENER_STOPPED/);
  const afterStop = displays.length;
  await send(b, 'fixture:chat');
  assert.equal(displays.length, afterStop);
  await cleanup();
  assert.equal(closeCount, 1);
  await cleanup();
  assert.equal(closeCount, 1, 'Cleanup is idempotent');
  console.log(JSON.stringify({
    ok: true, actualHostModule: 'src/host.mjs', actualSdk: sdkRoot, node: process.version,
    fakeModelCalls: calls, paidModelCalls: 0, networkRequests: 0, actualDesktopWindows: 0,
    desktopPlaybackFixture: 'no onPlayback; no-backpressure test seam', desktopPreferencesFixture: 'outbound settings verified; no onPreferences callback', desktopInputFixture: 'onChatSubmit + chat:result; real original-agent inbox and Chat visibility classifier', persistence: 'disabled; independent memory sessions',
    checks: ['JSON output schema', 'real execute agent context', 'lol_read connector', 'idempotent watch start', 'real lol_tune execution', 'empty tune queries settings', 'dynamic prompt preference', 'dataMode propagated to watcher and prompt', 'tool preferences synchronized to desktop', '500 ms delivery follows sampling automatically', 'fresh combined decision contract without reply queue', 'dynamic sampling and batching intervals', 'per-conversation settings isolation', 'batched event followup wakes model', 'completed-turn desktop reply with holdMs', 'silent clears', 'long output paginates without model repair', 'Unicode graphemes retained', 'manual long reply reaches bubble', 'mixed turn kept visible', 'original records preserved', 'desktop exact text uses original user-source conversation', 'desktop idle input and answer stay visible in Chat', 'desktop duplicate ACK avoids duplicate history/model call', 'desktop input during running model acknowledges without interrupting', 'desktop queued input uses normal next turn', 'desktop mixed automatic/manual turn stays visible', 'desktop uses no separate agent or model route', 'zero replyLength removes preference', 'unbound session isolation', 'rebinding stops old watcher', 'stale callback rejection', 'cross-session stop protection', 'stop unbinds replies', 'idempotent cleanup'],
  }, null, 2));
} finally {
  modelPause?.release.resolve();
  if (cleanup) await cleanup();
  for (const handle of handles) await handle.dispose();
  await root.fiber.dispose();
  clearTimeout(deadline);
}
