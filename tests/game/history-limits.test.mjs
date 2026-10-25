import test from 'node:test';
import assert from 'node:assert/strict';
import { createUpdateBatcher } from '../../src/game/batcher.mjs';
import { reverseDelta } from '../../src/game/delta.mjs';

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

const observation = (number, fields = {}) => ({ capturedAt: `time-${number}`, dataMode: 'detailed', hp: number, ...fields });
function update(number, fields) {
  return {
    id: `update-${number}`, type: 'lol_update', observedAt: `time-${number}`,
    observation: observation(number, fields), changes: [],
    events: [{ EventID: number, EventName: 'ChampionKill', EventTime: number }],
  };
}

function restore(current, step) {
  assert.equal(current.capturedAt, step.fromObservedAt, 'history must have no missing link');
  let result = structuredClone(current);
  for (const { op, path, value } of step.patch) {
    if (!path.length) result = structuredClone(value);
    else {
      const container = path.slice(0, -1).reduce((node, key) => node[key], result);
      if (op === 'delete') delete container[path.at(-1)];
      else container[path.at(-1)] = structuredClone(value);
    }
  }
  assert.equal(result.capturedAt, step.toObservedAt);
  return result;
}

function verifyHistory(batch, expectedStates, omittedStepCount) {
  const { history } = batch;
  assert.equal(history.encoding, 'reverse-set-delete-v1');
  assert.equal(history.base, 'observation');
  assert.equal(history.retainedSteps, history.steps.length);
  assert.equal(history.retainedBytes, Buffer.byteLength(JSON.stringify(history.steps), 'utf8'));
  assert.ok(history.retainedSteps <= 20);
  assert.ok(history.retainedBytes <= 64 * 1024);
  assert.equal(history.truncated, omittedStepCount > 0);
  assert.equal(history.omittedStepCount, omittedStepCount);
  assert.equal(history.steps.length, expectedStates.length);
  let state = batch.observation;
  history.steps.forEach((step, index) => {
    state = restore(state, step);
    assert.deepEqual(state, expectedStates[index]);
  });
  assert.equal(history.retainedFrom, state.capturedAt);
}

test('first observation explicitly reports complete empty history and the current reachable timestamp', async () => {
  const clock = new FakeClock();
  const batches = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => batches.push(batch) });
  batcher.push(update(0));
  await clock.advance(15000);
  verifyHistory(batches[0], [], 0);
  assert.equal(batches[0].history.retainedBytes, 2);
  batcher.stop();
});

test('a minute busy keeps the newest 20 contiguous steps, the latest full snapshot and every new event', async () => {
  const clock = new FakeClock();
  const batches = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => batches.push(batch) });
  batcher.setBusy(true);
  const updates = [];
  for (let i = 0; i <= 60; i++) {
    const item = update(i, { players: [{ name: `player-${i}`, inventory: [i, i + 1] }] });
    updates.push(item);
    batcher.push(item);
    await clock.advance(1000);
  }
  assert.equal(batches.length, 0);
  batcher.setBusy(false);
  await clock.advance(0);
  assert.equal(batches.length, 1);
  const batch = batches[0];
  verifyHistory(batch, updates.slice(40, 60).reverse().map(item => item.observation), 40);
  assert.deepEqual(batch.observation, updates[60].observation);
  assert.deepEqual(batch.events, updates.flatMap(item => item.events));
  assert.equal(batch.sourceUpdateIds.length, 61);
  batcher.stop();
});

test('byte cap prunes oldest steps before the step-count limit without breaking reverse reconstruction', async () => {
  const clock = new FakeClock();
  const batches = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => batches.push(batch) });
  const updates = Array.from({ length: 4 }, (_, i) => update(i, { text: String(i).repeat(30000) }));
  updates.forEach(item => batcher.push(item));
  await clock.advance(15000);
  verifyHistory(batches[0], [updates[2].observation, updates[1].observation], 1);
  assert.deepEqual(batches[0].observation, updates[3].observation);
  assert.deepEqual(batches[0].events, updates.flatMap(item => item.events));
  batcher.stop();
});

test('64 KiB includes JSON array brackets and keeps an exact-boundary step but drops one byte beyond it', async () => {
  const baselinePrevious = observation(0, { text: '' });
  const current = observation(1, { text: '' });
  // Keep a changed text field in the baseline patch when measuring its overhead.
  baselinePrevious.text = 'x';
  const step = {
    fromObservedAt: current.capturedAt, toObservedAt: baselinePrevious.capturedAt,
    patch: reverseDelta(current, baselinePrevious),
  };
  const fixedBytes = Buffer.byteLength(JSON.stringify([step]), 'utf8') - 1;
  for (const excess of [0, 1]) {
    const clock = new FakeClock();
    const batches = [];
    const batcher = createUpdateBatcher({ clock, deliver: batch => batches.push(batch) });
    const previous = update(0, { text: 'x'.repeat(65536 - fixedBytes + excess) });
    batcher.push(previous);
    batcher.push(update(1, { text: '' }));
    await clock.advance(15000);
    verifyHistory(batches[0], excess ? [] : [previous.observation], excess);
    assert.equal(batches[0].history.retainedBytes, excess ? 2 : 65536);
    assert.equal(batches[0].events.length, 2);
    batcher.stop();
  }
});

test('UTF-8 byte budget accounts for Chinese text rather than JavaScript string length', async () => {
  const clock = new FakeClock();
  const batches = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => batches.push(batch) });
  const previous = update(0, { text: '鲸'.repeat(24000) });
  const current = update(1, { text: '新' });
  const unboundedStep = {
    fromObservedAt: current.observation.capturedAt, toObservedAt: previous.observation.capturedAt,
    patch: reverseDelta(current.observation, previous.observation),
  };
  assert.ok(JSON.stringify([unboundedStep]).length < 65536);
  assert.ok(Buffer.byteLength(JSON.stringify([unboundedStep]), 'utf8') > 65536);
  batcher.push(previous);
  batcher.push(current);
  await clock.advance(15000);
  verifyHistory(batches[0], [], 1);
  assert.deepEqual(batches[0].observation, current.observation);
  batcher.stop();
});

test('an oversized newest link discards older links, then later samples form a new contiguous suffix', async () => {
  const clock = new FakeClock();
  const batches = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => batches.push(batch) });
  const updates = [
    update(0, { text: 'old' }), update(1, { text: '鲸'.repeat(24000) }),
    update(2, { text: 'recovered' }), update(3, { text: 'latest' }),
  ];
  updates.forEach(item => batcher.push(item));
  await clock.advance(15000);
  verifyHistory(batches[0], [updates[2].observation], 2);
  assert.deepEqual(batches[0].events, updates.flatMap(item => item.events));
  assert.deepEqual(batches[0].observation, updates[3].observation);
  batcher.stop();
});

test('failure retries immutable bounded history while the next buffer preserves its own cross-batch baseline and metadata', async () => {
  const clock = new FakeClock();
  const attempts = [];
  const batcher = createUpdateBatcher({ clock, deliver: batch => {
    attempts.push(structuredClone(batch));
    if (attempts.length === 1) {
      batch.history.steps.length = 0;
      batch.history.omittedStepCount = 999;
      batch.observation.hp = -1;
      return false;
    }
  } });
  for (let i = 0; i <= 24; i++) batcher.push(update(i));
  await clock.advance(15000);
  verifyHistory(attempts[0], Array.from({ length: 20 }, (_, i) => observation(23 - i)), 4);
  batcher.push(update(25));
  await clock.advance(15000);
  assert.deepEqual(attempts[0], attempts[1], 'retry must retain exact ID, body and truncation metadata');
  await clock.advance(15000);
  verifyHistory(attempts[2], [observation(24)], 0);
  assert.deepEqual(attempts[2].events, update(25).events);
  batcher.stop();
});

test('stop discards accumulated history and a fresh batcher has no baseline or omission metadata from the old run', async () => {
  const clock = new FakeClock();
  const batches = [];
  const first = createUpdateBatcher({ clock, deliver: batch => batches.push(batch) });
  first.setBusy(true);
  for (let i = 0; i < 50; i++) first.push(update(i));
  first.stop();
  assert.equal(first.status().bufferedUpdates, 0);
  assert.equal(first.status().bufferedEvents, 0);
  assert.equal(first.push(update(50)), false);
  await clock.advance(60000);
  assert.equal(batches.length, 0);
  const second = createUpdateBatcher({ clock, deliver: batch => batches.push(batch) });
  second.push(update(100));
  await clock.advance(15000);
  verifyHistory(batches[0], [], 0);
  assert.deepEqual(batches[0].observation, observation(100));
  second.stop();
});
