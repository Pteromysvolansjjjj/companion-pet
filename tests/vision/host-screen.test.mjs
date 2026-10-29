import test from 'node:test';
import assert from 'node:assert/strict';
import { mountCompanion } from '../../src/host.mjs';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function fakeVision() {
  const listeners = new Map();
  const calls = { start: 0, ocr: [], voice: [], stopVoice: 0, close: 0 };
  let frame = null;
  return {
    start() { calls.start++; return true; },
    send() { return true; },
    on(event, callback) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event).add(callback);
      return () => listeners.get(event)?.delete(callback);
    },
    latestFrame: () => frame,
    setOcr(value) { calls.ocr.push(value); return true; },
    setVoice(config) { calls.voice.push(config); return true; },
    stopVoice() { calls.stopVoice++; return true; },
    requestSnapshot() { return true; },
    status: () => ({ enabled: true, running: true, ready: true, frames: 1 }),
    close() { calls.close++; },
    // test helpers
    push(next) { frame = next; for (const callback of listeners.get('frame') ?? []) callback(next); },
    emit(event, payload) { for (const callback of listeners.get(event) ?? []) callback(payload); },
    calls,
  };
}

function fakeMobile() {
  const sent = [];
  return {
    sent,
    start() { this.started = true; return true; },
    async close() { this.closed = true; },
    send(message) { sent.push(message); return true; },
    status: () => ({ enabled: true, running: true, clients: 1 }),
    url: () => 'http://127.0.0.1:8790/?t=test',
  };
}

function fixture() {
  const tools = new Map(), listeners = new Map(), displayed = [], deliveries = [];
  let now = 0, timerId = 0, messageId = 0;
  const timers = new Map();
  const clock = {
    now: () => now,
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  async function tick(ms = 15000) {
    now += ms;
    for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); }
    for (let i = 0; i < 15; i++) await Promise.resolve();
  }
  const ctx = {
    tools: { register(def) { tools.set(def.name, def); return () => tools.delete(def.name); } },
    on(type, callback) { listeners.set(type, callback); return () => listeners.delete(type); },
    effect(factory) { this.cleanup = factory(); },
  };
  let preferencesCallback;
  const desktop = {
    start() {}, send(message) { displayed.push(message); return true; }, status: () => ({ running: true }),
    close() {}, onPreferences(callback) { preferencesCallback = callback; return () => { preferencesCallback = null; }; },
    onReady() { return () => {}; }, onChatSubmit() { return () => {}; },
  };
  const vision = fakeVision();
  const mobile = fakeMobile();
  const sdk = { defineTool: def => def, createUserMessage: input => ({ ...input, id: `fixture-${++messageId}`, role: 'user' }) };
  const cleanup = mountCompanion(ctx, sdk, { dataSource: 'screen' }, { vision, mobile, desktop, clock, persistStatus: false });
  const agent = {
    id: 'agent-A', session: { id: 'session-A' }, status: 'idle',
    followup: input => deliveries.push(input),
    ctx: { systemPrompt: { section: () => () => {} } },
  };
  const call = (name, args = {}) => tools.get(name).execute(args, { agent, signal: new AbortController().signal });
  const event = (session, value) => listeners.get('session/event')?.(session, value);
  return { tools, displayed, deliveries, vision, mobile, agent, call, cleanup, tick, event, preferences: request => preferencesCallback?.(request) };
}

const frame = (overrides = {}) => ({
  type: 'frame', id: 1, capturedAt: new Date().toISOString(),
  window: { title: 'League of Legends (TM) Client', width: 1920, height: 1080, foreground: true },
  durationMs: 380, regions: {}, fields: { gameTime: 601, allyKills: 6, enemyKills: 3, level: 9 },
  bars: {}, board: null,
  lines: [{ text: '10:01', region: 'top', score: 0.9 }],
  events: [],
  ...overrides,
});

test('the phone shows the same bubble the desktop pet shows', async t => {
  const f = fixture();
  t.after(async () => { await f.cleanup(); });
  await f.call('lol_watch_start');
  assert.equal(f.mobile.started, true, 'the phone page becomes reachable with the watch');

  const session = f.agent.session;
  f.event(session, { type: 'turn/start', data: { turn: 1 } });
  f.event(session, { type: 'assistant/message', data: { turn: 1, message: { content: [{ type: 'text', text: '对面打野在上路，别压线。' }] } } });
  f.event(session, { type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } });

  const bubble = f.mobile.sent.findLast(message => message.type === 'reply');
  assert.ok(bubble, 'the reply is forwarded to the phone');
  assert.deepEqual(bubble.lines, ['对面打野在上路，别压线。']);
  assert.equal(bubble.holdMs, 5000, 'the phone uses the same bubble hold preference');
});

test('screen mode starts the sidecar and delivers OCR text through the existing batched path', async t => {
  const f = fixture();
  t.after(async () => { await f.cleanup(); });
  await f.call('lol_watch_start');
  assert.equal(f.vision.calls.start, 1, 'the sidecar starts with the watch');
  assert.equal(f.vision.calls.ocr.at(-1), true, 'capture is enabled for the bound session');
  assert.equal(f.vision.calls.voice.length, 1, 'voice input is offered by default');
  assert.equal(f.vision.calls.voice[0].mode, 'push');
  assert.equal(f.vision.calls.voice[0].hotkey, 'F2');

  f.vision.push(frame());
  await delay(1200);
  await f.tick(15000);

  assert.equal(f.deliveries.length, 1, 'one batched turn, not one per sample');
  const payload = f.deliveries[0].content[0].text;
  assert.match(payload, /^\[LOL 实时数据\]/);
  assert.match(payload, /screen_ocr/);
  assert.match(payload, /"gameTime":601/);
  assert.match(payload, /"level":9/);
});

test('a recognised voice line becomes ordinary user input, and the echo uses the bubble', async t => {
  const f = fixture();
  t.after(async () => { await f.cleanup(); });
  await f.call('lol_watch_start');
  f.vision.emit('voice', { text: '这波先别打，等我回城', durationMs: 2100, latencyMs: 1700 });
  assert.equal(f.deliveries.length, 1, 'voice input bypasses the automatic interval');
  assert.deepEqual(f.deliveries[0].content, [{ type: 'text', text: '这波先别打，等我回城' }]);
  assert.deepEqual(f.deliveries[0].source, { kind: 'user' }, 'the model must see this as human input, not game data');
  const echo = f.displayed.findLast(message => message.type === 'reply');
  assert.ok(echo, 'the recognised text is shown back in the bubble');
  assert.deepEqual(echo.lines, ['这波先别打，等我回城']);
});

test('voice with nothing recognised never reaches the model', async t => {
  const f = fixture();
  t.after(async () => { await f.cleanup(); });
  await f.call('lol_watch_start');
  f.vision.emit('voice', { text: '   ', empty: true });
  assert.equal(f.deliveries.length, 0);
  assert.ok(f.displayed.some(message => message.type === 'status' && message.text.includes('没有听清')));
});

test('switching the data source stops OCR but keeps the session usable', async t => {
  const f = fixture();
  t.after(async () => { await f.cleanup(); });
  await f.call('lol_watch_start');
  await f.call('lol_tune', { dataSource: 'api' });
  assert.equal(f.vision.calls.ocr.at(-1), false, 'screen sampling stops when the API is selected');
  await f.call('lol_tune', { dataSource: 'screen', voiceMode: 'toggle', voiceHotkey: 'Ctrl+Alt+F2' });
  assert.equal(f.vision.calls.ocr.at(-1), true);
  assert.equal(f.vision.calls.voice.at(-1).mode, 'toggle');
  assert.equal(f.vision.calls.voice.at(-1).hotkey, 'Ctrl+Alt+F2');
  await f.call('lol_tune', { voiceMode: 'off' });
  assert.ok(f.vision.calls.stopVoice >= 1);
});

test('stopping the watch disables capture and voice', async t => {
  const f = fixture();
  t.after(async () => { await f.cleanup(); });
  await f.call('lol_watch_start');
  await f.call('lol_watch_stop');
  assert.equal(f.vision.calls.ocr.at(-1), false);
  assert.ok(f.vision.calls.stopVoice >= 1);
});
