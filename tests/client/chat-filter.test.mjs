import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { initialChatProjection, foldChatProjection } from '../../src/chat-projection.mjs';
import { classifyGameWindow, mountChatFilter } from '../../src/client/chat-filter.mjs';
import { context, source, loadProjectionSdk } from './sdk-fixture.mjs';

const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function turn(turn = 1, sourceKind = 'lol-companion', text = '<silent/>', offset = 0) {
  let seq = offset;
  const ev = (type, data, surfaceOp) => ({ type, seq: seq++, time: 1000 + seq, data, ...(surfaceOp ? { surfaceOp } : {}) });
  return [ev('turn/start', { turn }), ev('step/start', { turn, step: 1 }),
    ev('user/message', { id: 'input-' + turn, role: 'user', source: { kind: sourceKind }, content: [{ type: 'text', text: 'game data' }] }, 'append'),
    ev('assistant/message', { turn, step: 1, message: { id: 'reply-' + turn, role: 'assistant', source: { provider: 'test', model: 'fake' }, content: [{ type: 'text', text }] }, stream: [] }, 'append'),
    ev('step/end', { turn, step: 1, reason: { kind: 'completed' } }), ev('turn/end', { turn, reason: { kind: 'completed' } })];
}
const entries = events => events.map(event => ({ type: 'event', event }));
const project = events => events.reduce(foldChatProjection, initialChatProjection()).view;

function fixture() {
  const { conversation, chat, trajectory } = loadProjectionSdk();
  const ctx = context();
  const bindings = new Map();
  const providers = new Set();
  const sessions = { binding: id => bindings.get(id) };
  const ui = new conversation.UiConversation(ctx, sessions);
  chat.register(ctx); trajectory.register(ctx);
  ctx.uiSession = { provide(value) { providers.add(value); for (const binding of bindings.values()) value.resolve(binding);
    return () => providers.delete(value); } };
  function add(id, events, full = events) {
    const feed = source({ revision: 0, entries: entries(events), hasMore: full.length > events.length, change: { kind: 'replace' } });
    const projection = source(project(full));
    const binding = { sessionId: id, ctx: context(), eventSource: feed, session: { projections: { faceOf: () => projection } } };
    bindings.set(id, binding);
    const bound = ui.binding(binding); // Deliberately subscribe the SDK before our filter.
    bound.activate('chat'); bound.activate('trajectory');
    for (const provider of providers) provider.resolve(binding);
    return { binding, bound, feed, projection,
      nodes: () => bound.target('chat').getSnapshot().nodes.values(),
      release: () => { bindings.delete(id); binding.ctx.dispose(); },
      append(event, type = 'event') {
        const before = feed.getSnapshot(); const entry = { type, event };
        feed.set({ ...before, revision: before.revision + 1, entries: [...before.entries, entry], change: { kind: 'append', entries: [entry] } });
      },
      replace(events, full = events) {
        projection.set(project(full));
        const before = feed.getSnapshot();
        feed.set({ revision: before.revision + 1, entries: entries(events), hasMore: full.length > events.length, change: { kind: 'replace' } });
      },
    };
  }
  return { ctx, add, mount: () => mountChatFilter(ctx), dispose() { for (const value of bindings.values()) value.ctx.dispose(); ctx.dispose(); } };
}

test('provenance survives replay, ignores text and makes every non-runtime source manual', () => {
  const automatic = turn();
  const view = project(automatic);
  assert.deepEqual(view.hiddenTurns, [1]);
  assert.deepEqual(view.hiddenRanges, [{ turn: 1, startSeq: 0, endSeq: 5 }]);
  assert.deepEqual(project(JSON.parse(JSON.stringify(automatic))), view);
  for (const kind of ['user', 'sdk-test', undefined]) {
    const mixed = structuredClone(automatic);
    mixed.splice(3, 0, { type: 'user/message', seq: 2.5, surfaceOp: 'append', data: { source: { kind }, content: [] } });
    assert.deepEqual(project(mixed).hiddenTurns, []);
    assert.deepEqual(project(mixed).mixedTurns, [1]);
  }
  const manual = turn(2, 'user', '<silent/>');
  assert.deepEqual(project(manual).hiddenTurns, []);
  assert.ok([...classifyGameWindow(entries(automatic.slice(3)), view).values()].every(Boolean));
  assert.ok([...classifyGameWindow(entries(automatic), undefined).values()].every(value => !value));
});

test('real SDK Chat/Trajectory: complete automatic turns disappear, manual turns and raw events survive', async () => {
  const { conversation, chat, trajectory } = loadProjectionSdk();
  const ctx = context();
  const all = [...turn(), ...turn(2, 'user', 'manual answer', 6)];
  const feed = source({ revision: 0, entries: entries(all), hasMore: false, change: { kind: 'replace' } });
  const projection = source(project(all));
  const binding = { sessionId: 'a', ctx: context(), eventSource: feed, session: { projections: { faceOf: () => projection } } };
  const sessions = { binding: id => id === 'a' ? binding : undefined };
  const ui = new conversation.UiConversation(ctx, sessions);
  chat.register(ctx);
  trajectory.register(ctx);
  const bound = ui.binding(binding);
  bound.activate('chat'); bound.activate('trajectory');
  const before = bound.target('chat').getSnapshot();
  assert.ok(before.nodes.values().some(node => node.kind === 'turn-tail'));
  const trajectoryBefore = bound.target('trajectory').getSnapshot();
  let provider;
  ctx.uiSession = { provide(value) { provider = value; value.resolve(binding); return () => { provider = null; }; } };
  const originalEvents = JSON.stringify(all);
  const stop = mountChatFilter(ctx);
  await tick();
  const filtered = bound.target('chat').getSnapshot();
  const visible = filtered.nodes.values();
  assert.ok(!visible.some(node => node.location?.turn?.turn === 1), visible.map(node => node.kind).join(','));
  assert.ok(visible.some(node => node.kind === 'assistant-step' && node.location?.turn?.turn === 2));
  assert.equal(JSON.stringify(all), originalEvents);
  assert.deepEqual(bound.target('trajectory').getSnapshot(), trajectoryBefore);
  stop(); await tick();
  assert.ok(bound.target('chat').getSnapshot().nodes.values().some(node => node.location?.turn?.turn === 1));
  assert.equal(provider, null);
  binding.ctx.dispose(); ctx.dispose();
});

test('real SDK stream before filter listener, settlement, mixed input and session isolation', async () => {
  const f = fixture();
  const automatic = turn();
  const a = f.add('auto', automatic.slice(0, 3));
  const manual = f.add('manual', turn(1, 'user', '<silent/>'));
  const stop = f.mount(); await tick();
  assert.equal(a.nodes().length, 0);
  assert.ok(manual.nodes().some(node => node.kind === 'assistant-step'));
  const reasoning = { type: 'assistant/live-chunk', seq: 2.25, time: 1100, data: { turn: 1, step: 1, attemptId: 'live-a', index: 0, chunk: { type: 'reasoning-delta', index: 0, text: 'reasoning' } } };
  const reply = { type: 'assistant/live-chunk', seq: 2.5, time: 1101, data: { turn: 1, step: 1, attemptId: 'live-a', index: 1, chunk: { type: 'text-delta', index: 1, text: '<silent/>' } } };
  a.append(reasoning, 'transient'); a.append(reply, 'transient');
  assert.equal(a.nodes().length, 0, 'no provisional reasoning/reply is published');
  const final = { type: 'event', event: automatic[3] };
  const old = a.feed.getSnapshot();
  a.feed.set({ ...old, revision: old.revision + 1, entries: [...entries(automatic.slice(0, 3)), final],
    change: { kind: 'settle-assistant', attemptId: 'live-a', entry: final } });
  a.projection.set(project(automatic));
  a.append(automatic[4]); a.append(automatic[5]); await tick();
  assert.equal(a.nodes().length, 0, 'no completed footer or empty silent card');
  const mixed = [...turn(2, 'lol-companion', 'mixed answer')];
  mixed.splice(3, 0, { type: 'user/message', seq: 2.5, time: 1200, surfaceOp: 'append', data: { id: 'human-interjection', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'hello' }] } });
  a.replace(mixed); await tick();
  assert.ok(a.nodes().some(node => node.kind === 'user'));
  assert.ok(a.nodes().some(node => node.kind === 'assistant-step'));
  assert.ok(a.nodes().some(node => node.kind === 'turn-tail'));
  assert.ok(manual.nodes().some(node => node.kind === 'assistant-step'));
  a.release(); await tick();
  const reopened = f.add('auto', JSON.parse(JSON.stringify(automatic)));
  await tick(); assert.equal(reopened.nodes().length, 0, 'a new binding with reused ID is isolated from the old generation');
  stop(); await tick(); assert.ok(reopened.nodes().some(node => node.kind === 'assistant-step'));
  f.dispose();
});

test('desktop human input and its answer remain ordinary Chat nodes after original-session replay', async () => {
  const f = fixture();
  try {
    const human = turn(2, 'user', '好，我继续陪你。', 6);
    human[2].data.content[0].text = '[LOL 实时数据]\n这是我亲自输入的话 👩🏽‍💻';
    const events = [...turn(), ...human];
    const original = JSON.stringify(events);
    const view = f.add('original-conversation', events);
    f.mount(); await tick();
    assert.deepEqual(project(events).hiddenTurns, [1]);
    assert.ok(view.nodes().some(node => node.kind === 'user' && node.location?.turn?.turn === 2));
    assert.ok(view.nodes().some(node => node.kind === 'assistant-step' && node.location?.turn?.turn === 2));
    assert.ok(!view.nodes().some(node => node.location?.turn?.turn === 1));
    view.replace(JSON.parse(original)); await tick();
    assert.ok(view.nodes().some(node => node.kind === 'user' && node.location?.turn?.turn === 2));
    assert.ok(view.nodes().some(node => node.kind === 'assistant-step' && node.location?.turn?.turn === 2));
    assert.equal(JSON.stringify(events), original, 'display filtering never rewrites raw input or reply');
  } finally { f.dispose(); }
});

test('real SDK cold tail, cloned replace and historical prepend use host ranges', async () => {
  const f = fixture();
  const automatic = turn();
  const a = f.add('tail', automatic.slice(3), automatic);
  f.mount(); await tick();
  assert.equal(a.nodes().length, 0);
  const before = a.feed.getSnapshot();
  a.feed.set({ ...before, revision: before.revision + 1, entries: entries(automatic), hasMore: false,
    change: { kind: 'prepend', entries: entries(automatic.slice(0, 3)) } });
  await tick(); assert.equal(a.nodes().length, 0);
  a.replace(JSON.parse(JSON.stringify(automatic))); await tick();
  assert.equal(a.nodes().length, 0);
  const next = [...turn(1, 'user', 'manual'), ...turn(2, 'lol-companion', 'game', 6)];
  const partial = [...next.slice(0, 6), ...next.slice(8)];
  const classifications = classifyGameWindow(entries(partial), project(next));
  assert.equal(classifications.get(partial[6]), true, 'range fallback applies after a loaded preceding turn/end');
  f.dispose();
});

test('real SDK followup wake notice is hidden with its whole turn', async () => {
  const f = fixture();
  const automatic = turn();
  const input = automatic[2].data;
  automatic.unshift({ type: 'agent/inbox/spliced', data: { target: 'next-turn', start: 0, inserted: [input] } });
  automatic.splice(2, 0, { type: 'agent/inbox/spliced', data: { target: 'next-turn', start: 0, removedCount: 1, inserted: [] } });
  automatic.forEach((event, seq) => { event.seq = seq; event.time = 1000 + seq; });
  const a = f.add('wake', automatic);
  assert.ok(a.nodes().some(node => node.kind === 'turn-trigger'), 'fixture covers actual non-human followup notice');
  f.mount(); await tick(); assert.equal(a.nodes().length, 0);
  f.dispose();
});

test('real SDK a human interjection overrides stale hidden provenance before its wire update', async () => {
  const f = fixture();
  const automatic = turn();
  const a = f.add('mixed-live', automatic.slice(0, 4));
  f.mount(); await tick(); assert.equal(a.nodes().length, 0);
  a.append({ type: 'user/message', seq: 4, time: 1400, surfaceOp: 'append', data: { id: 'steer', role: 'user',
    source: { kind: 'user' }, content: [{ type: 'text', text: 'answer me here' }] } });
  await tick();
  assert.ok(a.nodes().some(node => node.kind === 'user'));
  assert.ok(a.nodes().some(node => node.kind === 'assistant-step'), 'existing response is restored with the mixed turn');
  f.dispose();
});

test('generated bundle registers the correct lazy DSH factory and service dependencies', () => {
  const manifest = JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf8'));
  const registrations = [];
  vm.runInNewContext(readFileSync(new URL('../../client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load: value => registrations.push(value) } }, queueMicrotask,
  });
  assert.equal(registrations.length, 1);
  assert.equal(registrations[0].id, manifest.name);
  assert.equal(manifest.exports['./client'], './client.js');
  const exported = registrations[0].factory(() => { throw new Error('bundle must not require an undeclared external'); });
  assert.equal(typeof exported.apply, 'function');
  assert.deepEqual(Array.from(exported.inject), ['uiConversation', 'uiSession']);
});
