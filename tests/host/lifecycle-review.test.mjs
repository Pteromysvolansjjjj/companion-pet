import test from 'node:test';
import assert from 'node:assert/strict';
import { mountCompanion } from '../../src/host.mjs';
import { parseSpeech } from '../../src/replies.mjs';

function fixture() {
  const tools = new Map();
  const listeners = new Map();
  const displayed = [];
  const watchers = [];
  let messageId = 0;
  let time = 0;
  let nextTimer = 0;
  const timers = new Map();
  const clock = { now: () => time, setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, at: time + ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  const ctx = {
    tools: { register(tool) { tools.set(tool.name, tool); return () => tools.delete(tool.name); } },
    on(name, handler) { listeners.set(name, handler); return () => listeners.delete(name); },
    effect(factory) { this.cleanup = factory(); },
  };
  const nextTurn = [];
  const nextStep = [];
  const agent = {
    id: 'A', session: { id: 'A' },
    followup(message) { nextTurn.push(message); },
    inbox: { nextTurn, nextStep, splice(target, start, count, inserted) { return (target === 'next-turn' ? nextTurn : nextStep).splice(start, count, ...inserted); } },
  };
  const cleanup = mountCompanion(ctx, {
    defineTool: value => value,
    createUserMessage: value => ({ ...value, role: 'user', id: `own-${++messageId}` }),
  }, {}, {
    clock, persistStatus: false,
    connector: { read: async () => ({ capturedAt: 'fixture', sections: {} }) },
    desktop: { start() {}, send: message => displayed.push(message), close() {}, status: () => ({ running: true }) },
    createWatcher(options) {
      const watcher = { emit: value => options.onUpdate(value), start() {}, async stop() {}, status: () => ({ running: true }) };
      watchers.push(watcher); return watcher;
    },
  });
  return {
    agent, displayed, watchers, cleanup,
    call: name => tools.get(name).execute({}, { agent, signal: new AbortController().signal }),
    event: event => listeners.get('session/event')(agent.session, event),
    async tick() { time += 15000; for (const [id, timer] of [...timers]) if (timer.at <= time) { timers.delete(id); timer.fn(); } for (let i = 0; i < 20; i++) await Promise.resolve(); },
  };
}

test('stopping then rebinding the same session cannot display the previous automatic turn', async () => {
  const f = fixture();
  try {
    await f.call('lol_watch_start');
    f.event({ type: 'turn/start', data: { turn: 7 } });
    f.event({ type: 'user/message', data: { id: 'old-automatic', source: { kind: 'lol-companion' } } });
    await f.call('lol_watch_stop');
    await f.call('lol_watch_start');
    const before = f.displayed.filter(message => message.type === 'reply').length;
    f.event({ type: 'assistant/message', data: { turn: 7, message: { content: [{ type: 'text', text: '这是停止前的旧回复' }] } } });
    f.event({ type: 'turn/end', data: { turn: 7, reason: { kind: 'completed' } } });
    assert.equal(f.displayed.filter(message => message.type === 'reply').length, before);
  } finally { await f.cleanup(); }
});

test('stop cancels only this plugins unclaimed input and preserves user prompts', async () => {
  const f = fixture();
  try {
    await f.call('lol_watch_start');
    f.agent.inbox.nextTurn.push({ id: 'user-waiting', source: { kind: 'user' } });
    f.agent.inbox.nextStep.push({ id: 'user-steering', source: { kind: 'user' } });
    await f.watchers[0].emit({ id: 'event-1', events: [{ EventID: 1 }] });
    await f.tick();
    assert.equal(f.agent.inbox.nextTurn.length, 2);
    await f.call('lol_watch_stop');
    assert.deepEqual(f.agent.inbox.nextTurn.map(message => message.id), ['user-waiting']);
    assert.deepEqual(f.agent.inbox.nextStep.map(message => message.id), ['user-steering']);
  } finally { await f.cleanup(); }
});

test('plain speech validation rejects short JSON and Markdown instead of displaying the wrappers', () => {
  for (const text of ['{"text":"你好"}', '**你好**', '# 标题', '```hello```']) assert.equal(parseSpeech(text), null, text);
});
