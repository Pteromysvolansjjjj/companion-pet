// Real JS bridge + real WPF dispatcher/JSONL protocol, with no visible window or user settings writes.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
import { setTimeout as delay } from 'node:timers/promises';
import { createDesktopBridge } from '../../src/desktop.mjs';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const wire = [], states = [], chatRequests = [];
let child;
const bridge = createDesktopBridge({ root, spawnProcess(executable, _args, options) {
  child = spawn(executable, ['--smoke-test'], options);
  createInterface({ input: child.stdout }).on('line', line => wire.push(JSON.parse(line)));
  return child;
} });
async function until(predicate) {
  const deadline = Date.now() + 6000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error('WPF protocol wait timed out: ' + JSON.stringify(bridge.status()));
    await delay(10);
  }
}
try {
  assert.equal(bridge.start(), true);
  bridge.onReady(() => bridge.send({ type: 'preferences', bindingId: 'fixture-bound', values: {
    pollIntervalMs: 500, decisionIntervalMs: 500, bubbleHoldMs: 700, replyLength: 0, dataMode: 'basic',
  } }));
  bridge.onPlayback(active => states.push(active));
  bridge.onChatSubmit(message => chatRequests.push(message));
  bridge.send({ type: 'clear' });
  bridge.send({ type: 'reply', lines: ['先读这一页', '接着读下一页'], holdMs: 500 });
  const afterSubmission = states.length;
  await until(() => wire.some(message => message.type === 'handled' && message.messageType === 'reply'));
  assert.equal(bridge.status().playback, true);
  assert.ok(!states.slice(afterSubmission).includes(false), 'earlier clear acknowledgement cannot release the submitted reply');
  await until(() => wire.some(message => message.type === 'handled' && message.messageType === 'preferences'));
  const settings = wire.find(message => message.type === 'handled' && message.messageType === 'preferences');
  assert.equal(settings.bindingId, 'fixture-bound');
  assert.equal(settings.decisionIntervalMs, 500);
  assert.equal(settings.pollIntervalMs, 500);
  assert.equal(settings.dataMode, 'basic');
  assert.equal(bridge.status().playback, true, 'preference synchronization cannot cancel the current response');
  bridge.send({ type: 'reply', lines: ['无效设置不应覆盖旧序列'], holdMs: 0 });
  await until(() => wire.some(message => message.type === 'invalid'));
  assert.equal(bridge.status().playback, true, 'nonfatal rejection retains active old playback');
  await until(() => bridge.status().playback === false);
  bridge.send({ type: 'reply', lines: ['随后隐藏'], holdMs: 30000 });
  await until(() => wire.filter(message => message.type === 'handled' && message.messageType === 'reply').length === 2);
  bridge.send({ type: 'hide' });
  await until(() => wire.some(message => message.type === 'handled' && message.messageType === 'hide'));
  assert.equal(bridge.status().playback, false);
  bridge.send({ type: 'show' });
  bridge.send({ type: 'reply', lines: ['输入期间继续显示'], holdMs: 30000 });
  await until(() => wire.filter(message => message.type === 'handled' && message.messageType === 'reply').length === 3);
  const typed = '  我这把想和你聊天，不要只报游戏数据。'.repeat(3) + '\n中文和 emoji 👩🏽‍💻  ';
  bridge.send({ type: 'chat:test', action: 'open' });
  bridge.send({ type: 'chat:test', action: 'text', text: typed });
  bridge.send({ type: 'chat:test', action: 'submit' });
  await until(() => chatRequests.length === 1);
  assert.equal(chatRequests[0].text, typed);
  assert.equal(chatRequests[0].bindingId, 'fixture-bound');
  await until(() => wire.findLast(message => message.type === 'handled')?.chatPending === true);
  bridge.send({ type: 'chat:result', bindingId: 'fixture-bound', requestId: chatRequests[0].requestId, accepted: false, error: '暂未接收，请重试。' });
  await until(() => wire.some(message => message.type === 'handled' && message.messageType === 'chat:result'));
  const failed = wire.findLast(message => message.type === 'handled');
  assert.equal(failed.chatVisible, true);
  assert.equal(failed.chatPending, false);
  assert.equal(failed.chatTextLength, typed.length);
  assert.equal(bridge.status().playback, true);
  bridge.send({ type: 'chat:test', action: 'submit' });
  await until(() => chatRequests.length === 2);
  bridge.send({ type: 'chat:test', action: 'timeout' });
  bridge.send({ type: 'chat:test', action: 'submit' });
  await until(() => chatRequests.length === 3);
  assert.deepEqual(chatRequests[2], chatRequests[1], 'timeout retries retain same identity and exact text');
  bridge.send({ type: 'chat:result', bindingId: 'fixture-bound', requestId: chatRequests[1].requestId, accepted: true });
  await until(() => wire.filter(message => message.type === 'handled' && message.messageType === 'chat:result').length === 2);
  const accepted = wire.findLast(message => message.type === 'handled');
  assert.equal(accepted.chatVisible, false);
  assert.equal(accepted.chatPending, false);
  assert.equal(accepted.chatTextLength, 0);
  assert.equal(bridge.status().playback, true, 'manual input acknowledgement does not change current bubble');
  console.log(JSON.stringify({ ok: true, realBridgeAndRenderer: true, visibleWindows: 0, paidModelCalls: 0,
    checks: ['pending clear correlation', 'real renderer preference synchronization', 'two-page real timer completion', 'rejected command preserves playback', 'hide cancels sequence', 'real composer text submission', 'failed chat keeps draft', 'timeout retries same request', 'accepted chat clears and hides without cancelling playback'] }, null, 2));
} finally {
  bridge.close();
  if (child && child.exitCode === null) await new Promise(resolve => child.once('exit', resolve));
}
