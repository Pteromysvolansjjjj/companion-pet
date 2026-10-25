import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough, Writable } from 'node:stream';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createDesktopBridge } from '../../src/desktop.mjs';

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'lol-desktop-test-'));
  mkdirSync(join(root, 'dist'));
  writeFileSync(join(root, 'dist', 'LolCompanion-0.7.1.exe'), 'test-double-only');
  const children = [], errors = [];
  const bridge = createDesktopBridge({
    root,
    onError: code => errors.push(code),
    spawnProcess(executable, args, options) {
      assert.equal(executable, join(root, 'dist', 'LolCompanion-0.7.1.exe'));
      assert.equal(options.windowsHide, true);
      assert.deepEqual(options.stdio, ['pipe', 'pipe', 'pipe']);
      const child = new EventEmitter();
      child.messages = [];
      child.commands = [];
      child.stdin = new Writable({ write(chunk, encoding, done) { const message = JSON.parse(chunk.toString('utf8')); child.commands.push(message); const { commandId, ...content } = message; child.messages.push(content); done(); } });
      child.stdout = new PassThrough();
      child.stderr = new PassThrough();
      child.killed = false;
      child.protocol = message => child.stdout.write(JSON.stringify(message) + '\n');
      child.finish = () => { child.emit('exit', 0); child.stdout.end(); child.stderr.end(); };
      child.kill = () => { child.killed = true; child.finish(); return true; };
      children.push(child);
      return child;
    },
  });
  t.after(() => {
    bridge.close();
    for (const child of children) {
      if (!child.killed) child.kill();
      child.stdin.destroy();
    }
    rmSync(root, { recursive: true, force: true });
  });
  return { bridge, children, errors };
}

test('ordinary reply/status/clear never launch the renderer, even with a legacy launch flag', t => {
  const { bridge, children } = fixture(t);
  for (const type of ['reply', 'status', 'clear']) {
    assert.equal(bridge.send({ type, text: 'no implicit launch' }), false);
    assert.equal(bridge.send({ type, text: 'no implicit launch' }, { launch: true }), false);
  }
  assert.equal(children.length, 0);
});

test('explicit start is idempotent and flushes latest pending display states when ready', t => {
  const { bridge, children } = fixture(t);
  assert.equal(bridge.start(), true);
  assert.equal(bridge.start(), true);
  assert.equal(children.length, 1);
  bridge.send({ type: 'reply', text: 'earlier' });
  bridge.send({ type: 'status', text: 'connected' });
  bridge.send({ type: 'reply', text: 'latest' });
  assert.deepEqual(children[0].messages, []);
  children[0].protocol({ type: 'ready', protocolVersion: 1 });
  assert.deepEqual(children[0].messages, [{ type: 'status', text: 'connected' }, { type: 'reply', text: 'latest' }]);
  assert.equal(bridge.status().ready, true);
});

test('tray exit immediately rejects traffic and later replies/status do not reopen the pet', t => {
  const { bridge, children } = fixture(t);
  bridge.start();
  const first = children[0];
  first.protocol({ type: 'ready' });
  first.protocol({ type: 'exit', reason: 'tray-exit' });
  assert.equal(bridge.status().running, false);
  assert.equal(bridge.send({ type: 'reply', text: 'during teardown' }), false);
  first.finish();
  assert.equal(bridge.send({ type: 'reply', text: 'after tray exit' }), false);
  assert.equal(bridge.send({ type: 'status', text: 'still watching' }), false);
  assert.equal(children.length, 1);
  assert.deepEqual(first.messages, []);
});

test('explicit show can reopen a user-exited renderer without replaying dropped replies', t => {
  const { bridge, children } = fixture(t);
  bridge.start();
  children[0].protocol({ type: 'ready' });
  children[0].protocol({ type: 'exit', reason: 'menu-exit' });
  children[0].finish();
  bridge.send({ type: 'reply', text: 'discarded while closed' });
  assert.equal(bridge.send({ type: 'show' }), true);
  assert.equal(children.length, 2);
  children[1].protocol({ type: 'ready' });
  assert.deepEqual(children[1].messages, [{ type: 'show' }]);
  assert.equal(bridge.send({ type: 'reply', text: 'new reply' }), true);
  assert.deepEqual(children[1].messages[1], { type: 'reply', text: 'new reply' });
});

test('explicit start replaces an exiting renderer; stale child events cannot alter the new one', t => {
  const { bridge, children } = fixture(t);
  bridge.start();
  children[0].protocol({ type: 'exit', reason: 'tray-exit' });
  assert.equal(bridge.start(), true);
  assert.equal(children.length, 2);
  assert.equal(children[0].killed, true);
  children[1].protocol({ type: 'ready' });
  children[0].emit('error', new Error('late old process error'));
  children[0].emit('exit', 0);
  assert.equal(bridge.status().running, true);
  assert.equal(bridge.status().ready, true);
  assert.equal(bridge.status().error, null);
});

test('spawn failure permits a later explicit retry but never a reply-triggered retry', t => {
  const { bridge, children, errors } = fixture(t);
  bridge.start();
  children[0].emit('error', new Error('spawn failed'));
  assert.equal(bridge.status().running, false);
  assert.equal(bridge.send({ type: 'reply', text: 'no retry' }), false);
  assert.equal(children.length, 1);
  assert.deepEqual(errors, ['DESKTOP_START_FAILED']);
  assert.equal(bridge.start(), true);
  assert.equal(children.length, 2);
});

test('plugin disposal is terminal and cannot be undone by start, show or normal traffic', t => {
  const { bridge, children } = fixture(t);
  bridge.start();
  children[0].protocol({ type: 'ready' });
  bridge.close();
  assert.deepEqual(children[0].messages, [{ type: 'shutdown' }]);
  assert.equal(children[0].stdin.writableEnded, true);
  assert.equal(bridge.start(), false);
  assert.equal(bridge.send({ type: 'show' }), false);
  assert.equal(bridge.send({ type: 'reply', text: 'ignored' }), false);
  assert.equal(children.length, 1);
});

test('playback acknowledgements, pending clear, pipe failures and tray exit release host scheduling', t => {
  const { bridge, children } = fixture(t);
  const states = [];
  const unsubscribe = bridge.onPlayback(value => states.push(value));
  bridge.start();
  bridge.send({ type: 'reply', lines: ['待播放'] });
  assert.equal(bridge.status().playback, true);
  bridge.send({ type: 'clear' });
  assert.equal(bridge.status().playback, false);
  children[0].protocol({ type: 'ready', protocolVersion: 3 });
  assert.deepEqual(children[0].messages, [{ type: 'clear' }]);
  bridge.send({ type: 'reply', lines: ['第一句', '第二句'], holdMs: 500 });
  children[0].protocol({ type: 'playback', active: true, commandId: children[0].commands.at(-1).commandId });
  assert.equal(states.at(-1), true);
  children[0].protocol({ type: 'playback', active: false, commandId: children[0].commands.at(-1).commandId });
  assert.equal(states.at(-1), false);
  bridge.send({ type: 'reply', lines: ['退出前'] });
  children[0].protocol({ type: 'exit' });
  assert.equal(states.at(-1), false);
  bridge.start(); children[1].protocol({ type: 'ready' });
  bridge.send({ type: 'reply', lines: ['管道故障前'] });
  children[1].stdin.emit('error', new Error('fixture pipe failure'));
  assert.equal(states.at(-1), false);
  unsubscribe();
});

test('stale playback acknowledgements cannot release a newer reply; nonfatal errors preserve playback', t => {
  const { bridge, children } = fixture(t);
  bridge.start();
  bridge.send({ type: 'clear' });
  bridge.send({ type: 'reply', lines: ['长回复第一页', '长回复第二页'] });
  children[0].protocol({ type: 'ready', protocolVersion: 3 });
  const [clear, first] = children[0].commands;
  children[0].protocol({ type: 'playback', active: false, commandId: clear.commandId });
  assert.equal(bridge.status().playback, true);
  children[0].protocol({ type: 'playback', active: true, commandId: first.commandId });
  children[0].stderr.write('nonfatal diagnostic');
  assert.equal(bridge.status().playback, true);
  bridge.send({ type: 'reply', lines: [''], holdMs: 0 });
  const rejected = children[0].commands.at(-1);
  children[0].protocol({ type: 'invalid', commandId: rejected.commandId, currentCommandId: first.commandId, active: true });
  assert.equal(bridge.status().playback, true);
  children[0].protocol({ type: 'playback', active: false, commandId: first.commandId });
  assert.equal(bridge.status().playback, false);
  bridge.send({ type: 'reply', lines: ['新回复'] });
  children[0].protocol({ type: 'playback', active: false, commandId: first.commandId });
  assert.equal(bridge.status().playback, true);
});

test('unwritable stdin during ready flush releases pending playback', t => {
  const { bridge, children } = fixture(t);
  bridge.start(); bridge.send({ type: 'reply', lines: ['还没有送达'] });
  assert.equal(bridge.status().playback, true);
  children[0].stdin.end();
  children[0].protocol({ type: 'ready', protocolVersion: 3 });
  assert.equal(bridge.status().playback, false);
});

test('settings protocol forwards only current renderer requests and refreshes on ready', t => {
  const { bridge, children } = fixture(t);
  const requests = [];
  const removeRequest = bridge.onPreferences(message => requests.push(message));
  const removeReady = bridge.onReady(() => bridge.send({ type: 'preferences', bindingId: 'bound', values: { dataMode: 'detailed' } }));
  bridge.start(); children[0].protocol({ type: 'ready' });
  assert.equal(children[0].messages.at(-1).type, 'preferences');
  children[0].protocol({ type: 'preferences:set', bindingId: 'bound', requestId: 'ui-1', changes: { decisionIntervalMs: 500 } });
  assert.equal(requests.length, 1);
  children[0].protocol({ type: 'exit' });
  children[0].protocol({ type: 'preferences:set', bindingId: 'bound', requestId: 'stale' });
  assert.equal(requests.length, 1);
  removeRequest(); removeReady();
});

test('chat requests preserve human text, require live renderer and leave playback unchanged', t => {
  const { bridge, children } = fixture(t);
  const requests = [];
  const remove = bridge.onChatSubmit(message => requests.push(message));
  const request = { type: 'chat:submit', bindingId: 'bound', requestId: 'request-1', text: '  这把别当教练\n陪我聊天 👩🏽‍💻  ' };
  assert.equal(bridge.send({ type: 'chat:result', bindingId: request.bindingId, requestId: request.requestId, accepted: true }), false);
  bridge.start();
  const first = children[0];
  first.protocol(request);
  assert.equal(requests.length, 0, 'unready process cannot submit');
  first.protocol({ type: 'ready' });
  bridge.send({ type: 'reply', lines: ['播放中的回复'] });
  const displayId = first.commands.at(-1).commandId;
  first.protocol(request);
  assert.deepEqual(requests, [request]);
  bridge.send({ type: 'chat:result', bindingId: 'bound', requestId: 'request-1', accepted: true });
  assert.equal(bridge.status().playback, true, 'chat acknowledgement does not release whole-reply waiting');
  assert.equal(first.commands.at(-1).commandId, undefined);
  first.protocol({ type: 'playback', commandId: displayId, active: false });
  assert.equal(bridge.status().playback, false);
  first.protocol({ type: 'exit' });
  first.protocol(request);
  assert.equal(requests.length, 1, 'exiting process cannot submit');
  bridge.start();
  children[1].protocol({ type: 'ready' });
  first.stdout.emit('data', Buffer.from(JSON.stringify(request) + '\n'));
  assert.equal(requests.length, 1, 'replaced process cannot submit');
  children[1].protocol({ ...request, requestId: 'request-2' });
  assert.equal(requests.length, 2);
  remove();
  children[1].protocol({ ...request, requestId: 'request-3' });
  assert.equal(requests.length, 2);
  bridge.onChatSubmit(message => requests.push(message));
  bridge.close();
  children[1].protocol({ ...request, requestId: 'request-4' });
  assert.equal(requests.length, 2, 'closing bridge cannot submit');
});


