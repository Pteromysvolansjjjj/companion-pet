import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createPreferenceStore } from '../../src/preferences.mjs';

test('saved preferences survive reload and remain isolated by session without saving game or memory data', t => {
  const root = mkdtempSync(join(tmpdir(), 'lol-settings-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  let store = createPreferenceStore({ root, config: { decisionIntervalMs: 12000 } });
  store.set('A', { replyLength: 0, pollIntervalMs: 250, bubbleHoldMs: 30000, dataMode: 'basic' });
  store.set('B', { replyLength: 150 });
  store.set('__proto__', { replyLength: 60 });
  store = createPreferenceStore({ root, config: { decisionIntervalMs: 20000 } });
  assert.equal(store.get('A').replyLength, 0);
  assert.equal(store.get('A').dataMode, 'basic');
  assert.equal(store.get('A').decisionIntervalMs, 20000, 'unspecified values track defaults');
  assert.equal(store.get('B').replyLength, 150);
  assert.equal(store.get('__proto__').replyLength, 60);
  assert.equal(store.get('new').replyLength, 20);
  const saved = JSON.parse(readFileSync(join(root, 'data/conversation-settings.json'), 'utf8'));
  assert.deepEqual(Object.keys(saved).sort(), ['sessions', 'version']);
  assert.deepEqual(Object.keys(saved.sessions.A).sort(), ['bubbleHoldMs', 'dataMode', 'pollIntervalMs', 'replyLength']);
});

test('invalid preference edits are atomic and do not reset valid settings', () => {
  const store = createPreferenceStore({ root: '.', persist: false });
  store.set('A', { replyLength: 80 });
  for (const patch of [{ replyLength: -1 }, { replyLength: 3.5 }, { pollIntervalMs: 249 }, { decisionIntervalMs: 499 }, { dataMode: 'important-only' }, { dataMode: true }, { bubbleHoldMs: 30001 }, { pollIntervalMs: Infinity }, { replyLength: 30, personality: 'coach' }]) {
    assert.throws(() => store.set('A', patch));
    assert.equal(store.get('A').replyLength, 80);
  }
});

test('basic fallback preserves explicit detailed choices and config defaults without rewriting saved data', t => {
  const root = mkdtempSync(join(tmpdir(), 'lol-settings-default-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  let store = createPreferenceStore({ root });
  assert.equal(store.get('new').dataMode, 'basic');
  store.set('explicit', { dataMode: 'detailed', decisionIntervalMs: 500 });
  store.set('unspecified', { replyLength: 80 });
  const path = join(root, 'data/conversation-settings.json');
  const before = readFileSync(path, 'utf8');
  store = createPreferenceStore({ root });
  assert.equal(store.get('explicit').dataMode, 'detailed');
  assert.equal(store.get('explicit').decisionIntervalMs, 500);
  assert.equal(store.get('unspecified').dataMode, 'basic');
  assert.equal(store.get('unspecified').replyLength, 80);
  store = createPreferenceStore({ root, config: { dataMode: 'detailed' } });
  assert.equal(store.get('new').dataMode, 'detailed');
  assert.equal(store.get('unspecified').dataMode, 'detailed');
  assert.equal(readFileSync(path, 'utf8'), before, 'loading never migrates or rewrites saved preferences');
  store.set('basic-choice', { dataMode: 'basic' });
  assert.equal(createPreferenceStore({ root, config: { dataMode: 'detailed' } }).get('basic-choice').dataMode, 'basic');
});
