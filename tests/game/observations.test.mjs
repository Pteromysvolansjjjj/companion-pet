import test from 'node:test';
import assert from 'node:assert/strict';
import { createGameWatcher, createUpdateBatcher, detectUpdate, projectObservation } from '../../src/game/index.mjs';

class FakeClock {
  time = 0;
  sequence = 0;
  tasks = new Map();
  now = () => this.time;
  setTimeout = (fn, delay) => { const id = ++this.sequence; this.tasks.set(id, { at: this.time + delay, fn }); return id; };
  clearTimeout = id => this.tasks.delete(id);
  async flush() { for (let i = 0; i < 25; i++) await Promise.resolve(); }
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

const detectDetailedUpdate = (snapshot, previous) => detectUpdate(snapshot, previous, { dataMode: 'detailed' });

const kill = { EventID: 9, EventTime: 10, EventName: 'ChampionKill' };
function sample(at, { hp = 100, gold = 500, events = [], gameId = 1, timer = 20, additions = {} } = {}) {
  return {
    capturedAt: `sample-${at}`,
    sources: { lcu: { available: true }, live: { available: true } },
    sections: {
      phase: { status: 'ok', data: 'InProgress' },
      gameflow: { status: 'ok', data: { gameData: { gameId } } },
      live: { status: 'ok', data: {
        activePlayer: { currentGold: gold, championStats: { currentHealth: hp, maxHealth: 100 } },
        gameData: { gameTime: 100, unknownClock: timer },
        allPlayers: Array.from({ length: 10 }, (_, index) => ({ riotId: `Player-${index}`, championName: `Champion-${index}`, scores: { kills: 0, deaths: 0, assists: 0 }, items: [{ itemID: 1001, description: 'unchanged equipment description' }] })),
        events: { Events: events }, ...additions,
      } },
    },
    self: { status: 'matched', playerIndex: 0 },
  };
}

// An independent reader of the public wire contract, intentionally used only by tests.
function restoreHistory(batch) {
  assert.equal(batch.history.encoding, 'reverse-set-delete-v1');
  assert.equal(batch.history.base, 'observation');
  let state = structuredClone(batch.observation);
  const states = [structuredClone(state)];
  for (const step of batch.history.steps) {
    assert.equal(state.capturedAt, step.fromObservedAt);
    for (const { op, path, value } of step.patch) {
      if (path.length === 0) { state = structuredClone(value); continue; }
      const container = path.slice(0, -1).reduce((node, key) => node[key], state);
      if (op === 'delete') delete container[path.at(-1)];
      else container[path.at(-1)] = structuredClone(value);
    }
    assert.equal(state.capturedAt, step.toObservedAt);
    states.push(structuredClone(state));
  }
  return states;
}

test('detector includes gold-only, unknown field, source and timer changes, but deduplicates timestamp/key-order-only changes', () => {
  const original = sample(0);
  const first = detectDetailedUpdate(original);
  const unchanged = sample(1);
  unchanged.sources = { live: { available: true }, lcu: { available: true } };
  assert.equal(detectDetailedUpdate(unchanged, first.next).update, null);
  for (const newer of [sample(1, { gold: 501 }), sample(1, { timer: 19 }), sample(1, { hp: 50 }), sample(1, { additions: { futureApiField: ['a', 2] } })]) {
    assert.deepEqual(detectDetailedUpdate(newer, first.next).update.observation, projectObservation(newer, 'detailed'));
  }
  const sourceChanged = structuredClone(original);
  sourceChanged.sources.live.available = false;
  assert.ok(detectDetailedUpdate(sourceChanged, first.next).update);
  const credentialsOnly = structuredClone(original);
  credentialsOnly.sections.live.data.authToken = 'secret';
  assert.equal(detectDetailedUpdate(credentialsOnly, first.next).update, null);
});

test('latest observation plus deltas exactly preserves health drop/restore, all fields and array changes with less duplication', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  const samples = [sample(0), sample(1, { hp: 20, gold: 501, additions: { effect: { name: 'slow', remaining: 2 } } }), sample(2, { hp: 100, gold: 502, additions: { inventoryChanges: [1, { item: 'potion' }] } })];
  samples[1].sections.live.data.allPlayers[3].scores.assists = 1;
  let previous;
  for (const [index, observation] of samples.entries()) {
    const detected = detectDetailedUpdate(observation, previous);
    previous = detected.next;
    assert.equal(batcher.push({ id: `s-${index}`, ...detected.update }), true);
  }
  await clock.advance(15000);
  assert.deepEqual(delivered[0].observation, projectObservation(samples[2], 'detailed'));
  assert.deepEqual(restoreHistory(delivered[0]), samples.toReversed().map(value => projectObservation(value, 'detailed')));
  assert.ok(JSON.stringify(delivered[0]).length < JSON.stringify(samples).length);
  assert.equal(Object.hasOwn(delivered[0], 'context'), false);
  batcher.stop();
});

test('each later batch contains a complete state and a reversible path back to the previous batch', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  const first = detectDetailedUpdate(sample(0));
  batcher.push({ id: 'first', ...first.update });
  await clock.advance(15000);
  const next = detectDetailedUpdate(sample(1, { hp: 10, events: [kill] }), first.next);
  batcher.push({ id: 'next', ...next.update });
  await clock.advance(15000);
  assert.deepEqual(restoreHistory(delivered[1]), [next.update.observation, first.update.observation]);
  assert.deepEqual(delivered[1].events, [kill]);
  assert.deepEqual(delivered[0].history.steps, []);
  batcher.stop();
});

test('automatic batches never repeat event bodies in observations or reverse history across append, outage and new game', async () => {
  for (const dataMode of ['basic', 'detailed']) {
    const clock = new FakeClock();
    const delivered = [];
    const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
    const old = { EventID: 8, EventTime: 9, EventName: 'ChampionKill', VictimName: 'old-event-only-player' };
    const newer = { ...kill, VictimName: 'new-event-only-player' };
    const restarted = { EventID: 8, EventTime: 1, EventName: 'GameStart', Name: 'new-game-only-marker' };
    const outage = sample(2); outage.sections.live = { status: 'unavailable', reason: 'not_listening' };
    const samples = [sample(0, { events: [old] }), sample(1, { events: [old, newer] }), outage,
      sample(3, { events: [old, newer] }), sample(4, { gameId: 2, events: [restarted] })];
    let previous;
    const expected = [];
    for (const [index, raw] of samples.entries()) {
      const detected = detectUpdate(raw, previous, { dataMode });
      previous = detected.next;
      expected.push(detected.update.observation);
      batcher.push({ id: `${dataMode}-${index}`, ...detected.update });
    }
    await clock.advance(15000);
    const batch = delivered[0];
    assert.deepEqual(batch.events, [newer, restarted]);
    assert.deepEqual(restoreHistory(batch), expected.toReversed());
    assert.deepEqual(batch.observation.sections.live.data.events, { eventCount: 1, lastEventId: 8, lastEventTime: 1, logIncluded: false });
    const stateTransport = JSON.stringify({ observation: batch.observation, history: batch.history });
    for (const forbidden of ['ChampionKill', 'GameStart', 'old-event-only-player', 'new-event-only-player', 'new-game-only-marker', '"Events"']) {
      assert.equal(stateTransport.includes(forbidden), false, `${dataMode}: ${forbidden} must travel only as a new event body`);
    }
    const wire = JSON.stringify(batch);
    assert.equal(wire.split('new-event-only-player').length - 1, 1);
    assert.equal(wire.split('new-game-only-marker').length - 1, 1);
    assert.equal(wire.includes('old-event-only-player'), false, 'midgame baseline does not send historical bodies');
    batcher.stop();
  }
});

test('busy batching preserves lossless observations and reused event IDs across game boundaries', async () => {
  const clock = new FakeClock();
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => delivered.push(batch) });
  batcher.setBusy(true);
  const first = detectDetailedUpdate(sample(0));
  const old = detectDetailedUpdate(sample(1, { hp: 0, events: [kill] }), first.next);
  const fresh = detectDetailedUpdate(sample(2, { gameId: 2, events: [kill] }), old.next);
  [first, old, fresh].forEach((detected, index) => batcher.push({ id: `b-${index}`, ...detected.update }));
  await clock.advance(15000);
  assert.equal(delivered.length, 0);
  batcher.setBusy(false);
  await clock.advance(15000);
  assert.deepEqual(restoreHistory(delivered[0]), [fresh.update.observation, old.update.observation, first.update.observation]);
  assert.deepEqual(delivered[0].events, [kill, kill]);
  assert.equal(delivered[0].sessionChanges[0].beforeEventIndex, 1);
  batcher.stop();
});

test('changing batching interval reschedules current timer and preserves pending acknowledgement without overlap', async () => {
  const clock = new FakeClock();
  let acknowledge;
  const delivered = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => { delivered.push(batch); if (delivered.length === 1) return new Promise(resolve => { acknowledge = resolve; }); } });
  batcher.push({ id: 'one', ...detectDetailedUpdate(sample(0)).update });
  await clock.advance(4000);
  assert.equal(batcher.setIntervalMs(5000), 5000);
  await clock.advance(999);
  assert.equal(delivered.length, 0);
  await clock.advance(1);
  assert.equal(delivered.length, 1);
  batcher.push({ id: 'two', ...detectDetailedUpdate(sample(1, { events: [kill] })).update, events: [kill] });
  batcher.setIntervalMs(1000);
  await clock.advance(3000);
  assert.equal(delivered.length, 1);
  assert.equal(batcher.status().bufferedUpdates, 2);
  acknowledge(false); await clock.flush();
  await clock.advance(1000);
  assert.deepEqual(delivered[1], delivered[0]);
  await clock.advance(1000);
  assert.deepEqual(delivered[2].events, [kill]);
  assert.equal(batcher.status().intervalMs, 1000);
  assert.equal(batcher.status().bufferedUpdates, 0);
  batcher.stop();
});

test('watcher interval changes reschedule sleep and retain the live event cursor without overlapping reads', async () => {
  const clock = new FakeClock();
  const updates = [];
  let reads = 0;
  let active = 0;
  let peak = 0;
  const watcher = createGameWatcher({ clock, dataMode: 'detailed', intervalMs: 10000, connector: { read: async () => {
    active++; peak = Math.max(peak, active); await Promise.resolve();
    const value = sample(reads++, { events: reads > 1 ? [kill, { ...kill, EventID: 10 }] : [kill], gold: reads + 500 });
    active--; return value;
  } }, onUpdate: update => updates.push(update) });
  watcher.start(); await clock.flush();
  assert.deepEqual(updates[0].events, []);
  await clock.advance(100);
  watcher.setIntervalMs(250);
  await clock.advance(149);
  assert.equal(reads, 1);
  await clock.advance(1);
  assert.equal(reads, 2);
  assert.deepEqual(updates[1].events, [{ ...kill, EventID: 10 }]);
  watcher.setIntervalMs(1000);
  await clock.advance(999);
  assert.equal(reads, 2);
  await clock.advance(1);
  assert.equal(reads, 3);
  assert.deepEqual(updates[2].events, []);
  assert.equal(peak, 1);
  await watcher.stop();
  assert.equal(clock.tasks.size, 0);
});

test('watcher data-mode changes preserve pending payloads and event cursors without restarting', async () => {
  const clock = new FakeClock();
  const updates = [];
  let reads = 0;
  const watcher = createGameWatcher({ clock, dataMode: 'detailed', intervalMs: 500, connector: { read: async () => sample(reads++, { events: reads > 1 ? [kill] : [], hp: 40 }) },
    onUpdate: update => { updates.push(update); return updates.length !== 1; } });
  watcher.start(); await clock.flush();
  assert.equal(updates[0].observation.dataMode, 'detailed');
  assert.equal(watcher.setDataMode('basic'), 'basic');
  assert.throws(() => watcher.setDataMode('unknown'), /dataMode/);
  assert.equal(watcher.status().dataMode, 'basic');
  await clock.advance(500);
  assert.deepEqual(updates[1], updates[0]);
  assert.equal(reads, 1, 'retry must retain the accepted detailed observation');
  await clock.advance(500);
  assert.equal(reads, 2);
  assert.equal(updates[2].observation.dataMode, 'basic');
  assert.deepEqual(updates[2].events, [kill]);
  assert.equal(Object.hasOwn(updates[2].observation.sections.live.data.activePlayer, 'championStats'), false);
  assert.equal(watcher.latestSnapshot.sections.live.data.activePlayer.championStats.currentHealth, 40);
  assert.deepEqual(watcher.latestSnapshot.sections.live.data.events.Events, [kill], 'direct-query snapshot retains cumulative bodies');
  watcher.setDataMode('detailed');
  await clock.advance(500);
  assert.equal(updates[3].observation.dataMode, 'detailed');
  assert.deepEqual(updates[3].events, []);
  await watcher.stop();
});
