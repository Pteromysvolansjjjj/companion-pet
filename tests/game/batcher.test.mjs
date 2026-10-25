import test from 'node:test';
import assert from 'node:assert/strict';
import { createUpdateBatcher } from '../../src/game/index.mjs';

class FakeClock {
  time = 0;
  sequence = 0;
  tasks = new Map();
  now = () => this.time;
  setTimeout = (fn, delay) => { const id = ++this.sequence; this.tasks.set(id, { at: this.time + delay, fn }); return id; };
  clearTimeout = id => this.tasks.delete(id);
  async flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
  async advance(milliseconds) {
    const until = this.time + milliseconds;
    await this.flush();
    for (;;) {
      const entry = [...this.tasks].sort((a, b) => a[1].at - b[1].at)[0];
      if (!entry || entry[1].at > until) break;
      const [id, task] = entry;
      this.time = task.at;
      this.tasks.delete(id);
      task.fn();
      await this.flush();
    }
    this.time = until;
    await this.flush();
  }
}

function update(id, { events = [], changes = [], context, ...rest } = {}) {
  return { id, type: 'lol_update', observedAt: `time-${id}`, initial: false, events, changes, ...(context ? { context } : {}), ...rest };
}
const event = (id, name = 'ChampionKill') => ({ EventID: id, EventTime: id, EventName: name });

test('first delivery waits 15 seconds and merges latest sections, self and context without losing event order', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  batcher.push(update('one', { initial: true, events: [event(1)], changes: [{ section: 'phase', status: 'ok', data: 'Lobby' }, { section: 'summoner', status: 'ok', data: { level: 50 } }], context: { self: { level: 1 }, gameData: { gameTime: 1 } }, self: { level: 1 } }));
  await clock.advance(1000);
  batcher.push(update('two', { events: [event(2), event(3)], changes: [{ section: 'phase', status: 'ok', data: 'InProgress' }], context: { self: { level: 2 }, gameData: { gameTime: 2 } }, self: { level: 2 } }));
  await clock.advance(13999);
  assert.equal(delivered.length, 0);
  await clock.advance(1);
  assert.equal(delivered.length, 1);
  const batch = delivered[0];
  assert.deepEqual(batch.events, [event(1), event(2), event(3)]);
  assert.deepEqual(batch.changes, [{ section: 'phase', status: 'ok', data: 'InProgress' }, { section: 'summoner', status: 'ok', data: { level: 50 } }]);
  assert.deepEqual(batch.context, { self: { level: 2 }, gameData: { gameTime: 2 } });
  assert.deepEqual(batch.self, { level: 2 });
  assert.equal(batch.initial, true);
  assert.deepEqual(batch.sourceUpdateIds, ['one', 'two']);
  assert.equal(batcher.status().bufferedEvents, 0);
  batcher.stop();
});

test('duplicate updates and events are deduplicated, but event IDs reused in a new game are preserved', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  const first = update('one', { events: [event(1)] });
  batcher.push(first); batcher.push(first);
  batcher.push(update('two', { events: [event(1)] }));
  batcher.push(update('three', { events: [event(1)], sessionChange: 'client_game_id_changed' }));
  await clock.advance(15000);
  assert.deepEqual(delivered[0].sourceUpdateIds, ['one', 'two', 'three']);
  assert.deepEqual(delivered[0].events, [event(1), event(1)]);
  assert.equal(delivered[0].sessionChanges[0].beforeEventIndex, 1);
  batcher.push(first);
  await clock.advance(15000);
  assert.equal(delivered.length, 1);
  batcher.stop();
});

test('busy turns keep accumulating every event and deliver one fresh merged batch immediately when overdue', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  batcher.setBusy(true);
  batcher.push(update('one', { events: [event(1)], context: { gameData: { gameTime: 1 } } }));
  await clock.advance(15000);
  batcher.push(update('two', { events: [event(2)], context: { gameData: { gameTime: 2 } } }));
  await clock.advance(15000);
  assert.equal(delivered.length, 0);
  assert.equal(batcher.status().bufferedEvents, 2);
  batcher.setBusy(false);
  assert.equal(delivered.length, 0);
  await clock.advance(0);
  assert.equal(delivered.length, 1);
  assert.deepEqual(delivered[0].events, [event(1), event(2)]);
  assert.deepEqual(delivered[0].context, { gameData: { gameTime: 2 } });
  batcher.stop();
});

test('events at 1, 2 and 5 seconds become one latest-state judgement when busy ends at 6 seconds', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, intervalMs: 1000, deliver: batch => delivered.push({ at: clock.now(), batch }) });
  batcher.setBusy(true);
  for (const at of [1000, 2000, 5000]) {
    await clock.advance(at - clock.now());
    batcher.push(update(`at-${at}`, { events: [event(at)], observation: { capturedAt: `at-${at}`, dataMode: 'detailed', hp: at / 100 } }));
  }
  await clock.advance(1000);
  assert.equal(delivered.length, 0);
  batcher.setBusy(false);
  await clock.advance(0);
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0].at, 6000);
  assert.deepEqual(delivered[0].batch.events, [event(1000), event(2000), event(5000)]);
  assert.equal(delivered[0].batch.observation.capturedAt, 'at-5000');
  assert.equal(delivered[0].batch.observation.hp, 50);
  assert.equal(delivered[0].batch.history.steps.length, 2);
  batcher.push(update('next', { events: [event(6100)] }));
  await clock.advance(999);
  assert.equal(delivered.length, 1);
  await clock.advance(1);
  assert.equal(delivered.length, 2);
  assert.equal(delivered[1].at, 7000, 'a fresh batch must still respect the attempt cadence');
  batcher.stop();
});

test('a failed acknowledgement retries the exact batch ID and preserves newer arrivals for a later batch', async () => {
  const clock = new FakeClock();
  const attempts = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => { attempts.push(batch); return attempts.length !== 1; } });
  batcher.push(update('one', { events: [event(1)] }));
  await clock.advance(15000);
  assert.equal(batcher.status().bufferedEvents, 1);
  assert.equal(batcher.status().lastError, 'delivery_failed');
  batcher.push(update('two', { events: [event(2)] }));
  await clock.advance(15000);
  assert.deepEqual(attempts[0], attempts[1]);
  assert.equal(batcher.status().bufferedEvents, 1);
  await clock.advance(15000);
  assert.deepEqual(attempts[2].events, [event(2)]);
  assert.equal(batcher.status().bufferedEvents, 0);
  batcher.stop();
});

test('slow acknowledgements never overlap deliveries or erase events collected while pending', async () => {
  const clock = new FakeClock();
  let acknowledge;
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => { delivered.push(batch); if (delivered.length === 1) return new Promise(resolve => { acknowledge = resolve; }); } });
  batcher.push(update('one', { events: [event(1)] }));
  await clock.advance(15000);
  batcher.push(update('two', { events: [event(2)] }));
  await clock.advance(45000);
  assert.equal(delivered.length, 1);
  assert.equal(batcher.status().bufferedEvents, 2);
  acknowledge(); await clock.flush();
  assert.equal(batcher.status().bufferedEvents, 1);
  await clock.advance(15000);
  assert.equal(delivered.length, 2);
  assert.deepEqual(delivered[1].events, [event(2)]);
  batcher.stop();
});

test('stop drops queued data, aborts pending delivery and prevents later timers or pushes', async () => {
  const clock = new FakeClock();
  let signal;
  let deliveries = 0;
  const batcher = createUpdateBatcher({ clock, deliver: (_batch, options) => { signal = options.signal; deliveries++; return new Promise(() => {}); } });
  batcher.push(update('one', { events: [event(1)] }));
  await clock.advance(15000);
  batcher.push(update('two', { events: [event(2)] }));
  batcher.stop();
  await clock.flush();
  assert.equal(signal.aborted, true);
  assert.equal(batcher.status().bufferedUpdates, 0);
  assert.equal(batcher.status().inFlight, false);
  assert.equal(batcher.push(update('three')), false);
  assert.equal(clock.tasks.size, 0);
  await clock.advance(60000);
  assert.equal(deliveries, 1);
});

test('each later batch contains only unconsumed section changes and events', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  batcher.push(update('one', { events: [event(1)], changes: [{ section: 'summoner', data: { level: 50 } }] }));
  await clock.advance(15000);
  batcher.push(update('two', { events: [event(2)], changes: [{ section: 'phase', data: 'InProgress' }] }));
  await clock.advance(15000);
  assert.deepEqual(delivered[1].events, [event(2)]);
  assert.deepEqual(delivered[1].changes, [{ section: 'phase', data: 'InProgress' }]);
  batcher.stop();
});

test('a disconnected latest context cannot inherit stale live fields', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  batcher.push(update('one', { events: [event(1)], context: { self: { status: 'matched' }, gameData: { gameTime: 500 }, allPlayers: [{ champion: 'Old' }] } }));
  batcher.push(update('two', { context: { self: { status: 'unresolved' } } }));
  await clock.advance(15000);
  assert.deepEqual(delivered[0].context, { self: { status: 'unresolved' } });
  assert.deepEqual(delivered[0].events, [event(1)]);
  batcher.stop();
});

test('new-session context resets without losing older unconsumed events or boundary order', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  batcher.setBusy(true);
  batcher.push(update('one', { events: [event(1)], context: { gameData: { gameTime: 500 } }, self: { champion: 'Old' } }));
  batcher.push(update('two', { events: [event(2, 'GameStart')], sessionChange: 'client_game_id_changed' }));
  batcher.setBusy(false);
  await clock.advance(15000);
  assert.equal(Object.hasOwn(delivered[0], 'context'), false);
  assert.equal(Object.hasOwn(delivered[0], 'self'), false);
  assert.deepEqual(delivered[0].events, [event(1), event(2, 'GameStart')]);
  assert.equal(delivered[0].sessionChanges[0].beforeEventIndex, 1);
  batcher.stop();
});

test('500 ms delivery remains serialized and keeps events while a model turn is busy', async () => {
  const clock = new FakeClock();
  const delivered = [];
  let acknowledge;
  const batcher = createUpdateBatcher({ clock, deliver: batch => {
    delivered.push(batch);
    if (delivered.length === 1) return new Promise(resolve => { acknowledge = resolve; });
  } });
  batcher.push(update('one', { events: [event(1)] }));
  assert.equal(batcher.setIntervalMs(500), 500);
  assert.throws(() => batcher.setIntervalMs(499), /at least 500/);
  await clock.advance(499);
  assert.equal(delivered.length, 0);
  await clock.advance(1);
  assert.equal(delivered.length, 1);
  batcher.push(update('two', { events: [event(2)] }));
  await clock.advance(1500);
  assert.equal(delivered.length, 1);
  batcher.setBusy(true);
  acknowledge(true); await clock.flush();
  await clock.advance(1000);
  assert.equal(delivered.length, 1);
  batcher.setBusy(false);
  await clock.advance(500);
  assert.equal(delivered.length, 2);
  assert.deepEqual(delivered[1].events, [event(2)]);
  assert.equal(batcher.status().intervalMs, 500);
  batcher.stop();
});
