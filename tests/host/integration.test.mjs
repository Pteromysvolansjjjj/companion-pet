import test from 'node:test';
import assert from 'node:assert/strict';
import { mountCompanion } from '../../src/host.mjs';
import { createReplyCollector } from '../../src/replies.mjs';
import { detectUpdate } from '../../src/game/watcher.mjs';

function fixture({ playback = false, read = async () => ({ capturedAt: 'now', sections: { phase: { status: 'ok', data: 'Lobby' } } }) } = {}) {
  const tools = new Map(), listeners = new Map(), displayed = [], watchers = [], deliveries = [];
  let now = 0, timerId = 0, messageId = 0;
  const timers = new Map();
  const clock = { now: () => now, setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, at: now + ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  async function tick(ms = 15000) {
    now += ms;
    for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); }
    for (let i = 0; i < 15; i++) await Promise.resolve();
  }
  const ctx = {
    tools: { register(def) { tools.set(def.name, def); return () => tools.delete(def.name); } },
    on(type, cb) { listeners.set(type, cb); return () => listeners.delete(type); },
    effect(factory) { this.cleanup = factory(); },
  };
  let playbackCallback, preferencesCallback, readyCallback, chatSubmitCallback;
  const desktop = { start() {}, send(message) { displayed.push(message); return true; }, status: () => ({ running: true }), close() { this.closed = true; },
    onPreferences(callback) { preferencesCallback = callback; return () => { preferencesCallback = null; }; },
    onReady(callback) { readyCallback = callback; return () => { readyCallback = null; }; },
    onChatSubmit(callback) { chatSubmitCallback = callback; return () => { chatSubmitCallback = null; }; },
  };
  if (playback) desktop.onPlayback = callback => { playbackCallback = callback; return () => { playbackCallback = null; }; };
  const connector = { read };
  const createWatcher = options => {
    const item = { options, intervalMs: options.intervalMs, dataMode: options.dataMode, setIntervalMs(ms) { this.intervalMs = ms; }, setDataMode(mode) { this.dataMode = mode; }, started: false, stopped: false, start() { this.started = true; }, async stop() { this.stopped = true; }, status() { return { running: this.started && !this.stopped, intervalMs: this.intervalMs, dataMode: this.dataMode }; } };
    watchers.push(item); return item;
  };
  const sdk = { defineTool: def => def, createUserMessage: input => ({ ...input, id: `fixture-${++messageId}`, role: 'user' }) };
  const cleanup = mountCompanion(ctx, sdk, {}, { connector, desktop, createWatcher, clock, persistStatus: false });
  const prompts = new Set();
  const agent = { id: 'agent-A', session: { id: 'session-A' }, followup: input => deliveries.push(input), ctx: { systemPrompt: { section(value) { prompts.add(value.text); return () => prompts.delete(value.text); } } } };
  const call = (name, args = {}, caller = agent) => tools.get(name).execute(args, { agent: caller, signal: new AbortController().signal });
  const event = (session, value) => listeners.get('session/event')(session, value);
  return { tools, listeners, displayed, watchers, deliveries, desktop, agent, call, event, cleanup, tick, prompts, playback: active => playbackCallback?.(active), preferences: request => preferencesCallback?.(request), ready: () => readyCallback?.(), chatSubmit: request => chatSubmitCallback?.(request), chatHandler: () => chatSubmitCallback };
}
const assistant = (turn, text, extra = {}) => ({ type: 'assistant/message', data: { turn, message: { content: [{ type: 'text', text }] }, ...extra } });
const completed = turn => ({ type: 'turn/end', data: { turn, reason: { kind: 'completed' } } });
const chatRequest = (f, requestId, text) => ({ type: 'chat:submit', bindingId: f.displayed.findLast(message => message.type === 'preferences').bindingId, requestId, text });

test('desktop input immediately sends exact unrestricted user text to the original conversation and acknowledges enqueue', async t => {
  const f = fixture(); t.after(f.cleanup);
  await f.call('lol_watch_start');
  const text = '  以后多说两句，别只说二十个字。\n我想继续跟原来的你聊天 🐋\n  ';
  const request = chatRequest(f, 'human-1', text);
  await f.chatSubmit(request);
  assert.equal(f.deliveries.length, 1, 'manual input does not wait for the 15-second automatic interval');
  assert.equal(f.watchers.length, 1, 'no second session or listener is started');
  assert.deepEqual(f.deliveries[0].content, [{ type: 'text', text }]);
  assert.deepEqual(f.deliveries[0].source, { kind: 'user' });
  assert.deepEqual(f.displayed.at(-1), { type: 'chat:result', bindingId: request.bindingId, requestId: request.requestId, accepted: true });
  const long = chatRequest(f, 'human-long', '字'.repeat(65536));
  await f.chatSubmit(long);
  assert.equal(f.deliveries[1].content[0].text.length, 65536, 'bubble page capacity never limits user input');
  assert.equal(f.displayed.at(-1).accepted, true);
});

test('manual input bypasses playback and queues while model is busy, with automatic data held until human work and playback finish', async t => {
  const f = fixture({ playback: true }); t.after(f.cleanup);
  f.agent.status = 'running';
  await f.call('lol_watch_start');
  f.playback(true);
  await f.chatSubmit(chatRequest(f, 'busy-human', '现在先听我说。'));
  assert.equal(f.deliveries.length, 1);
  assert.equal(f.displayed.at(-1).accepted, true);
  await f.watchers[0].options.onUpdate({ id: 'while-human-queued', events: [{ EventID: 10 }] });
  await f.tick(30000);
  assert.equal(f.deliveries.length, 1);
  f.agent.status = 'idle'; f.playback(false);
  await f.tick(30000);
  assert.equal(f.deliveries.length, 1, 'a queued human input holds automatic delivery even during an idle/maintenance boundary');
  f.agent.status = 'running';
  f.event(f.agent.session, { type: 'turn/start', data: { turn: 11 } });
  f.event(f.agent.session, { type: 'user/message', data: f.deliveries[0] });
  await f.tick(15000);
  assert.equal(f.deliveries.length, 1, 'admission does not release the active human turn');
  f.event(f.agent.session, assistant(11, '好，我听着。'));
  f.agent.status = 'idle'; f.event(f.agent.session, completed(11));
  await f.tick(15000);
  assert.equal(f.deliveries.length, 1, 'the resulting bubble still follows normal playback gating');
  f.playback(false); await f.tick(0);
  assert.equal(f.deliveries.length, 2);
  assert.equal(f.deliveries[1].source.kind, 'lol-companion');
});

test('accepted desktop requests retry idempotently and cannot reuse an ID for changed text', async t => {
  const f = fixture(); t.after(f.cleanup);
  await f.call('lol_watch_start');
  const request = chatRequest(f, 'same-request', '保留原始消息');
  await f.chatSubmit(request);
  const accepted = structuredClone(f.displayed.at(-1));
  await f.chatSubmit({ ...request });
  assert.deepEqual(f.displayed.at(-1), accepted);
  assert.equal(f.deliveries.length, 1);
  await f.chatSubmit({ ...request, text: '这已经是另一条了' });
  assert.equal(f.displayed.at(-1).accepted, false);
  assert.equal(f.deliveries.length, 1);
  await f.chatSubmit(request);
  assert.deepEqual(f.displayed.at(-1), accepted, 'conflicting request must not overwrite original acceptance');
});

test('lost desktop acknowledgement does not enqueue the accepted human message again', async t => {
  const f = fixture(); t.after(f.cleanup);
  await f.call('lol_watch_start');
  const request = chatRequest(f, 'lost-ack', '这条只应发送一次');
  const send = f.desktop.send;
  f.desktop.send = message => { if (message.type === 'chat:result') throw new Error('closed pipe'); return send(message); };
  await f.chatSubmit(request);
  assert.equal(f.deliveries.length, 1);
  f.desktop.send = send;
  await f.chatSubmit(request);
  assert.equal(f.deliveries.length, 1);
  assert.equal(f.displayed.at(-1).accepted, true);
});

test('malformed, empty, oversized and stale desktop input is rejected without touching the model', async t => {
  const f = fixture(); t.after(f.cleanup);
  await f.chatSubmit({ type: 'chat:submit', bindingId: 'unbound', requestId: 'one', text: 'hello' });
  assert.equal(f.displayed.at(-1).accepted, false);
  await f.call('lol_watch_start');
  const request = chatRequest(f, 'valid', 'hello');
  for (const invalid of [
    { text: '' }, { text: ' \t\n' }, { text: '\uD800' }, { text: 'x'.repeat(65537) }, { text: { value: 'hello' } },
    { requestId: '' }, { requestId: 'x'.repeat(129) }, { requestId: '\uD800' }, { bindingId: 'stale' }, { type: 'other' },
  ]) {
    await f.chatSubmit({ ...request, ...invalid });
    assert.equal(f.displayed.at(-1).accepted, false);
  }
  assert.equal(f.deliveries.length, 0);
});

test('failed followup gives a safe rejection and a later retry with the same ID can succeed', async t => {
  const f = fixture(); t.after(f.cleanup);
  await f.call('lol_watch_start');
  const request = chatRequest(f, 'retry-after-failure', '请收到这条');
  f.agent.followup = () => { throw new Error('secret raw filesystem details'); };
  await f.chatSubmit(request);
  assert.equal(f.displayed.at(-1).accepted, false);
  assert.equal(JSON.stringify(f.displayed.at(-1)).includes('secret'), false);
  assert.equal((await f.call('lol_read')).plugin.batching.busy, false);
  f.agent.followup = message => f.deliveries.push(message);
  await f.chatSubmit(request);
  assert.equal(f.displayed.at(-1).accepted, true);
  assert.equal(f.deliveries.length, 1);
});

test('stop removes only automatic inputs and preserves an accepted desktop human message in the original inbox', async t => {
  const f = fixture(); t.after(f.cleanup);
  const inbox = { nextTurn: [], nextStep: [], splice(target, start, count, inserted) { (target === 'next-turn' ? this.nextTurn : this.nextStep).splice(start, count, ...inserted); } };
  f.agent.inbox = inbox;
  f.agent.followup = message => { f.deliveries.push(message); inbox.nextTurn.push(message); };
  await f.call('lol_watch_start');
  await f.watchers[0].options.onUpdate({ id: 'automatic-before-stop' }); await f.tick();
  const request = chatRequest(f, 'manual-before-stop', '停止监听也别删我说的话');
  await Promise.all([f.chatSubmit(request), f.call('lol_watch_stop')]);
  assert.equal(f.deliveries.length, 2);
  assert.deepEqual(inbox.nextTurn, [f.deliveries[1]]);
  assert.equal(inbox.nextTurn[0].source.kind, 'user');
  await f.chatSubmit(request);
  assert.equal(f.displayed.at(-1).accepted, false);
  assert.equal(f.deliveries.length, 2);
  await f.call('lol_watch_start');
  await f.chatSubmit(request);
  assert.equal(f.displayed.at(-1).accepted, false, 'old binding cannot submit after restarting the same session');
  assert.deepEqual(inbox.nextTurn, [f.deliveries[1]]);
});

test('session disposal and plugin cleanup reject old desktop handlers and release subscriptions', async () => {
  const f = fixture();
  await f.call('lol_watch_start');
  const request = chatRequest(f, 'disposed', '已关闭的对话不应接收');
  const oldHandler = f.chatHandler();
  f.listeners.get('session/disposed')(f.agent.session);
  await oldHandler(request);
  assert.equal(f.displayed.at(-1).accepted, false);
  assert.equal(f.deliveries.length, 0);
  await assert.rejects(f.call('lol_watch_start'), /supported agent API/);
  await f.cleanup();
  assert.equal(f.chatHandler(), null);
  await oldHandler(request);
  assert.equal(f.deliveries.length, 0);
  assert.equal(f.displayed.at(-1).accepted, false);
});

test('quiescence releases a cancelled pending human input without claiming or deleting session history', async t => {
  const f = fixture(); t.after(f.cleanup);
  let release;
  f.agent.whenIdle = () => new Promise(resolve => { release = resolve; });
  await f.call('lol_watch_start');
  await f.chatSubmit(chatRequest(f, 'cancelled-pending', 'pending'));
  await f.watchers[0].options.onUpdate({ id: 'after-cancel' });
  await f.tick();
  assert.equal(f.deliveries.length, 1);
  release(); await Promise.resolve(); await f.tick(0);
  assert.equal(f.deliveries.length, 2);
  assert.deepEqual(f.deliveries[0].content, [{ type: 'text', text: 'pending' }]);
});

test('read does not bind or awaken a conversation', async () => {
  const f = fixture(); const value = await f.call('lol_read', { section: 'phase' });
  assert.equal(value.result.data, 'Lobby'); assert.equal(f.watchers.length, 0); assert.equal(f.deliveries.length, 0);
  await f.cleanup();
});

test('long playback delivers latest state and all new events once with bounded history, while direct read keeps full log', async t => {
  const oldEvent = { EventID: 0, EventTime: 0, EventName: 'GameStart' };
  let snapshot = { capturedAt: 'sample-0', sections: { live: { status: 'ok', data: {
    gameData: { gameTime: 10 }, activePlayer: { currentGold: 0 }, events: { Events: [oldEvent] },
  } } } };
  const f = fixture({ playback: true, read: async () => snapshot });
  t.after(f.cleanup);
  await f.call('lol_tune', { dataMode: 'detailed', decisionIntervalMs: 500 });
  await f.call('lol_watch_start');
  f.playback(true);
  let previous;
  for (let i = 0; i <= 61; i++) {
    if (i) snapshot = { ...snapshot, capturedAt: `sample-${i}`, sections: { live: { status: 'ok', data: {
      gameData: { gameTime: 10 + i }, activePlayer: { currentGold: i },
      events: { Events: [...snapshot.sections.live.data.events.Events, { EventID: i, EventTime: i, EventName: 'ChampionKill', VictimName: `victim-${i}` }] },
    } } } };
    const detected = detectUpdate(snapshot, previous, { dataMode: f.watchers[0].dataMode });
    previous = detected.next;
    await f.watchers[0].options.onUpdate({ id: `sample-${i}`, ...detected.update });
    await f.tick(1000);
  }
  assert.equal(f.deliveries.length, 0, 'no model input while entire response is playing');
  f.playback(false);
  await f.tick(0);
  assert.equal(f.deliveries.length, 1);
  const batch = JSON.parse(f.deliveries[0].content[0].text.split('\n').at(-1));
  assert.equal(batch.observation.sections.live.data.activePlayer.currentGold, 61);
  assert.equal(batch.events.length, 61);
  assert.deepEqual(batch.events.map(event => event.EventID), Array.from({ length: 61 }, (_, i) => i + 1));
  assert.equal(batch.history.truncated, true);
  assert.equal(batch.history.omittedStepCount, 41);
  assert.equal(batch.history.steps.length, 20);
  assert.ok(batch.history.retainedBytes <= 65536);
  assert.equal(JSON.stringify(batch.observation).includes('VictimName'), false);
  assert.equal(JSON.stringify(batch.history).includes('VictimName'), false);
  const result = await f.call('lol_read', { section: 'live' });
  assert.equal(result.result.data.events.Events.length, 62, 'explicit read still provides complete API event log');
});
test('watch binds exactly the calling session and enqueues game facts without persona instructions', async () => {
  const f = fixture(); await f.call('lol_watch_start'); await f.call('lol_watch_start');
  assert.equal(f.watchers.length, 1);
  const update = { id: 'run:1', events: [{ EventName: 'ChampionKill', VictimName: 'self' }] };
  await f.watchers[0].options.onUpdate(update, {});
  assert.equal(f.deliveries.length, 0); await f.tick();
  assert.equal(f.deliveries.length, 1); assert.equal(f.deliveries[0].source.kind, 'lol-companion');
  const delivered = JSON.parse(f.deliveries[0].content[0].text.split('\n').at(-1));
  assert.deepEqual(delivered.events, update.events); assert.deepEqual(delivered.sourceUpdateIds, [update.id]);
  await f.cleanup();
});
test('only final reply in bound conversation reaches desktop; silent clears', async () => {
  const f = fixture(); await f.call('lol_watch_start'); f.displayed.length = 0;
  f.event({ id: 'session-other' }, assistant(1, 'other private message'));
  f.event({ id: 'session-other' }, completed(1));
  f.event(f.agent.session, assistant(1, 'final text'));
  assert.equal(f.displayed.length, 0);
  f.event(f.agent.session, completed(1));
  assert.deepEqual(f.displayed, [{ type: 'reply', lines: ['final text'], holdMs: 5000 }]);
  f.event(f.agent.session, assistant(2, '  <silent/>\n')); f.event(f.agent.session, completed(2));
  assert.deepEqual(f.displayed[1], { type: 'clear' });
  await f.cleanup();
});
test('interrupted, tool-call and reasoning messages never become bubble text', () => {
  const display = []; const c = createReplyCollector(message => display.push(message));
  c.accept(assistant(1, 'partial', { interrupted: true })); c.accept(completed(1));
  c.accept({ type: 'assistant/message', data: { turn: 2, message: { content: [{ type: 'text', text: 'running command' }, { type: 'tool-call', name: 'bash' }, { type: 'reasoning', text: 'private' }] } } }); c.accept(completed(2));
  c.accept(assistant(3, 'failed')); c.accept({ type: 'turn/end', data: { turn: 3, reason: { kind: 'error' } } });
  assert.deepEqual(display, []);
});
test('switching sessions cancels old watcher and rejects stale deliveries; other session cannot stop it', async () => {
  const f = fixture(); await f.call('lol_watch_start');
  const other = { id: 'agent-B', session: { id: 'session-B' }, followup() {} };
  const denied = await f.call('lol_watch_stop', {}, other); assert.equal(denied.stopped, false);
  await f.call('lol_watch_start', {}, other); assert.equal(f.watchers[0].stopped, true);
  await assert.rejects(f.watchers[0].options.onUpdate({ id: 'stale' }, {}), /LISTENER_STOPPED/);
  await f.cleanup(); assert.equal(f.desktop.closed, true); assert.equal(f.tools.size, 0);
});
test('followup failure is retained by batcher; stop aborts future delivery', async () => {
  const f = fixture(); f.agent.followup = () => { throw new Error('HOST_BUSY'); };
  await f.call('lol_watch_start'); assert.equal(await f.watchers[0].options.onUpdate({ id: '1' }, {}), true); await f.tick();
  const first = await f.call('lol_read'); assert.equal(first.plugin.batching.lastError, 'delivery_failed');
  f.agent.followup = input => f.deliveries.push(input); await f.tick(); assert.equal(f.deliveries.length, 1);
  await f.call('lol_watch_stop'); await assert.rejects(f.watchers[0].options.onUpdate({ id: '2' }, {}), /LISTENER_STOPPED/);
  await f.cleanup();
});

test('busy turns retain merged updates and stopping drops pending batches', async () => {
  const f = fixture(); await f.call('lol_watch_start');
  f.event(f.agent.session, { type: 'turn/start', data: { turn: 4 } });
  await f.watchers[0].options.onUpdate({ id: 'a', events: [{ EventID: 1 }] });
  await f.tick(); assert.equal(f.deliveries.length, 0);
  await f.watchers[0].options.onUpdate({ id: 'b', events: [{ EventID: 2 }] });
  f.event(f.agent.session, completed(4)); await f.tick();
  assert.equal(f.deliveries.length, 1);
  assert.equal(JSON.parse(f.deliveries[0].content[0].text.split('\n').at(-1)).events.length, 2);
  await f.watchers[0].options.onUpdate({ id: 'c' });
  await f.call('lol_watch_stop'); await f.tick(); assert.equal(f.deliveries.length, 1);
  await f.cleanup();
});

test('concurrent starts leave one listener and unloading cannot resurrect it', async () => {
  const f = fixture();
  const other = { id: 'agent-B', session: { id: 'session-B' }, followup() {} };
  await Promise.all([f.call('lol_watch_start'), f.call('lol_watch_start', {}, other)]);
  assert.equal(f.watchers.filter(w => w.started && !w.stopped).length, 1);
  const anotherStart = f.call('lol_watch_start');
  const closing = f.cleanup();
  await assert.rejects(anotherStart, /unloaded/);
  await closing;
  assert.equal(f.watchers.filter(w => w.started && !w.stopped).length, 0);
});

test('conversation tuning updates live intervals and prompt without losing buffered facts', async () => {
  const f = fixture();
  try {
    await f.call('lol_watch_start');
    await f.watchers[0].options.onUpdate({ id: 'kept', events: [{ EventID: 88 }] });
    const value = await f.call('lol_tune', { pollIntervalMs: 500, decisionIntervalMs: 2000, replyLength: 0, bubbleHoldMs: 700 });
    assert.equal(value.active, true);
    assert.equal(f.watchers.length, 1);
    assert.equal(f.watchers[0].intervalMs, 500);
    assert.equal(f.prompts.size, 1);
    assert.ok([...f.prompts][0].includes('回复总字数不限'));
    await f.tick(2000);
    assert.equal(f.deliveries.length, 1);
    assert.equal(JSON.parse(f.deliveries[0].content[0].text.split('\n').at(-1)).events[0].EventID, 88);
    f.event(f.agent.session, assistant(8, '字'.repeat(65))); f.event(f.agent.session, completed(8));
    assert.equal(f.displayed.at(-1).lines.join(''), '字'.repeat(65));
    assert.equal(f.displayed.at(-1).holdMs, 700);
    await assert.rejects(f.call('lol_tune', { decisionIntervalMs: 0 }), /integer/);
    assert.equal((await f.call('lol_tune')).preferences.decisionIntervalMs, 2000);
    await f.call('lol_watch_stop'); assert.equal(f.prompts.size, 0);
    await f.call('lol_watch_start'); assert.equal(f.watchers[1].intervalMs, 500);
  } finally { await f.cleanup(); }
});

test('tuning another conversation cannot change or bind the active listener', async () => {
  const f = fixture();
  try {
    await f.call('lol_watch_start');
    const other = { session: { id: 'other' }, followup() {} };
    const value = await f.call('lol_tune', { replyLength: 200, decisionIntervalMs: 1000 }, other);
    assert.equal(value.active, false);
    assert.equal((await f.call('lol_tune')).preferences.replyLength, 20);
    assert.equal((await f.call('lol_read')).plugin.batching.intervalMs, 15000);
    await f.call('lol_watch_start', {}, other);
    assert.equal((await f.call('lol_tune', {}, other)).preferences.replyLength, 200);
  } finally { await f.cleanup(); }
});

test('bubble playback delays model delivery while preserving samples; clear releases it', async () => {
  const f = fixture({ playback: true });
  try {
    await f.call('lol_watch_start');
    f.event(f.agent.session, assistant(1, '字'.repeat(85))); f.event(f.agent.session, completed(1));
    assert.equal(f.displayed.at(-1).lines.length, 5);
    await f.watchers[0].options.onUpdate({ id: 'during-playback-1', events: [{ EventID: 1 }] });
    await f.tick(); assert.equal(f.deliveries.length, 0);
    f.playback(true);
    await f.watchers[0].options.onUpdate({ id: 'during-playback-2', events: [{ EventID: 2 }] });
    await f.tick(); assert.equal(f.deliveries.length, 0);
    f.playback(false); await f.tick();
    assert.equal(f.deliveries.length, 1);
    assert.equal(JSON.parse(f.deliveries[0].content[0].text.split('\n').at(-1)).events.length, 2);
    f.event(f.agent.session, assistant(2, '还有两句\n继续说')); f.event(f.agent.session, completed(2));
    await f.watchers[0].options.onUpdate({ id: 'after-clear' });
    f.event(f.agent.session, assistant(3, '<silent/>')); f.event(f.agent.session, completed(3));
    await f.tick(); assert.equal(f.deliveries.length, 2);
  } finally { await f.cleanup(); }
});

test('closed desktop never stalls collection and manual idle fallback works without status events', async () => {
  const f = fixture({ playback: true });
  try {
    f.agent.whenIdle = () => Promise.resolve();
    await f.call('lol_watch_start');
    f.desktop.send = () => false;
    f.event(f.agent.session, { type: 'turn/start', data: { turn: 9 } });
    f.event(f.agent.session, assistant(9, '桌宠关闭也不会卡死'));
    f.event(f.agent.session, completed(9));
    await f.watchers[0].options.onUpdate({ id: 'manual-finished' });
    await Promise.resolve(); await f.tick();
    assert.equal(f.deliveries.length, 1);
  } finally { await f.cleanup(); }
});

test('desktop settings apply to the current conversation, acknowledge truth, and reject stale binding requests', async () => {
  const f = fixture();
  try {
    await f.call('lol_watch_start');
    const initial = f.displayed.findLast(message => message.type === 'preferences');
    assert.equal(initial.values.dataMode, 'basic');
    assert.ok(initial.bindingId);
    await f.preferences({ bindingId: initial.bindingId, requestId: 'ui-1', changes: { decisionIntervalMs: 500, dataMode: 'basic', bubbleHoldMs: 800 } });
    assert.equal(f.displayed.at(-1).requestId, 'ui-1');
    assert.equal(f.displayed.at(-1).values.pollIntervalMs, 500);
    assert.equal(f.displayed.at(-1).values.dataMode, 'basic');
    assert.equal(f.watchers[0].dataMode, 'basic');
    assert.equal((await f.call('lol_tune')).preferences.decisionIntervalMs, 500);
    await f.preferences({ bindingId: initial.bindingId, requestId: 'invalid', changes: { decisionIntervalMs: 499, replyLength: 123 } });
    assert.ok(f.displayed.at(-1).error);
    assert.equal(f.displayed.at(-1).values.replyLength, 20, 'invalid compound edits must be atomic');
    f.ready(); assert.equal(f.displayed.at(-1).values.dataMode, 'basic', 'reopened desktop receives current settings');
    const other = { session: { id: 'other' }, followup() {} };
    await f.call('lol_watch_start', {}, other);
    const otherSnapshot = f.displayed.findLast(message => message.type === 'preferences');
    assert.notEqual(otherSnapshot.bindingId, initial.bindingId);
    await f.preferences({ bindingId: initial.bindingId, requestId: 'old-window', changes: { replyLength: 999 } });
    assert.equal((await f.call('lol_tune', {}, other)).preferences.replyLength, 20);
    await f.call('lol_watch_stop', {}, other);
    assert.equal(f.displayed.at(-1).bindingId, null);
    await f.preferences({ bindingId: otherSnapshot.bindingId, requestId: 'after-stop', changes: { decisionIntervalMs: 1000 } });
    assert.equal((await f.call('lol_read')).plugin.watching, false);
    assert.equal(f.deliveries.length, 0, 'UI changes must not themselves invoke the model');
  } finally { await f.cleanup(); }
});

test('three kills during one displayed reply cause one fresh decision after playback, never queued per-event replies', async () => {
  const f = fixture({ playback: true });
  try {
    await f.call('lol_tune', { decisionIntervalMs: 500 });
    await f.call('lol_watch_start');
    const emit = (id, kills) => f.watchers[0].options.onUpdate({ id, events: [{ EventID: kills, EventName: 'ChampionKill' }], context: { kills, latest: id } });
    await f.tick(500);
    await emit('kill-at-1', 1); await f.tick(500);
    assert.equal(f.deliveries.length, 1);
    f.event(f.agent.session, { type: 'turn/start', data: { turn: 1 } });
    f.event(f.agent.session, assistant(1, '第一句正在显示')); f.event(f.agent.session, completed(1));
    await f.tick(1000); await emit('kill-at-2', 2);
    await f.tick(3000); await emit('kill-at-5', 3);
    await f.tick(1000);
    assert.equal(f.deliveries.length, 1, 'while speaking the model receives no new automatic requests');
    assert.equal(f.displayed.filter(message => message.type === 'reply').length, 1);
    f.playback(false); await f.tick(0);
    assert.equal(f.deliveries.length, 2, 'elapsed minimum interval permits one fresh decision immediately');
    const latest = JSON.parse(f.deliveries[1].content[0].text.split('\n').at(-1));
    assert.equal(latest.context.kills, 3);
    assert.equal(latest.context.latest, 'kill-at-5');
    assert.deepEqual(latest.events.map(event => event.EventID), [2, 3]);
    assert.ok([...f.prompts][0].includes('不把旧事件逐条排队补播'));
    f.event(f.agent.session, { type: 'turn/start', data: { turn: 2 } });
    f.event(f.agent.session, assistant(2, '<silent/>')); f.event(f.agent.session, completed(2));
    await f.tick(1000); assert.equal(f.deliveries.length, 2, 'consumed events do not produce a later backlog');
  } finally { await f.cleanup(); }
});
