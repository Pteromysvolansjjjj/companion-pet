import test from 'node:test';
import assert from 'node:assert/strict';
import { reverseDelta } from '../../src/game/delta.mjs';

function replay(current, patch) {
  let result = structuredClone(current);
  function assertDense(value) {
    if (Array.isArray(value)) {
      for (let index = 0; index < value.length; index++) assert.equal(Object.hasOwn(value, index), true, 'patch must not introduce an array hole');
      value.forEach(assertDense);
    } else if (value && typeof value === 'object') Object.values(value).forEach(assertDense);
  }
  for (const { op, path, value } of patch) {
    if (!path.length) result = structuredClone(value);
    else {
      const container = path.slice(0, -1).reduce((node, key) => node[key], result);
      if (op === 'delete') delete container[path.at(-1)];
      else container[path.at(-1)] = structuredClone(value);
    }
    assertDense(result);
  }
  return result;
}

test('array append reversal keeps an accumulated event log without copying its unchanged elements', () => {
  const previous = Array.from({ length: 100 }, (_, index) => ({ EventID: index, EventName: 'ChampionKill', VictimName: `Player-${index}` }));
  const current = [...previous, { EventID: 100, EventName: 'ChampionKill', VictimName: 'NewVictim' }];
  const patch = reverseDelta(current, previous);
  assert.deepEqual(patch, [{ op: 'set', path: ['length'], value: 100 }]);
  assert.deepEqual(replay(current, patch), previous);
  assert.ok(JSON.stringify(patch).length < JSON.stringify(previous).length / 50);
});

test('array empty/extend/shrink operations restore all elements without introducing holes', () => {
  for (const [current, previous] of [
    [[], [1, null, { name: 'potion' }]],
    [[1, null, { name: 'potion' }], []],
    [[1], [1, 2, 3]],
    [[1, 2, 3], [1]],
    [[], []],
  ]) {
    const patch = reverseDelta(current, previous);
    assert.deepEqual(replay(current, patch), previous);
    assert.equal(patch.some(operation => operation.path.length === 0), false, 'an array size change must not replace the whole array');
  }
});

test('array size changes also restore shifted, removed and changed element content', () => {
  for (const [current, previous] of [
    [['inserted', 'old-first', 'old-second'], ['old-first', 'old-second']],
    [['tail'], ['head', 'tail']],
    [[{ id: 2, hp: 10 }, { id: 3 }], [{ id: 1, hp: 100 }]],
    [[true, { field: 'now' }], [null, ['then'], 5]],
  ]) assert.deepEqual(replay(current, reverseDelta(current, previous)), previous);
});

test('nested arrays use length paths on the correct container and preserve arbitrary object fields', () => {
  const current = { players: [{ inventory: [{ id: 1 }, { id: 2 }], 'length': 'label' }, { inventory: [] }], matrix: [[1], []] };
  const previous = { players: [{ inventory: [{ id: 9 }], 'length': 'older label' }], matrix: [[1, 2], [null, 3], [4]] };
  const patch = reverseDelta(current, previous);
  assert.deepEqual(replay(current, patch), previous);
  assert.ok(patch.some(operation => JSON.stringify(operation.path) === JSON.stringify(['players', 0, 'inventory', 'length'])));
  assert.ok(patch.some(operation => JSON.stringify(operation.path) === JSON.stringify(['players', 'length'])));
});
