import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { request } from 'node:http';
import { createMobileBridge } from '../../src/mobile/server.mjs';

const root = new URL('../../', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');

async function startBridge(overrides = {}) {
  const bridge = createMobileBridge({ root, port: 0, host: '127.0.0.1', ...overrides });
  bridge.start();
  // listen() is async: wait until the status reports a bound port.
  for (let i = 0; i < 50 && !bridge.status().running; i++) await new Promise(r => setTimeout(r, 20));
  const { port } = bridge.status();
  assert.ok(port > 0, 'server did not bind');
  return { bridge, port };
}

function get(port, path) {
  return new Promise((resolve, reject) => {
    const req = request({ host: '127.0.0.1', port, path, method: 'GET' }, res => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', reject);
    req.end();
  });
}

test('requests without the token are rejected', async t => {
  const { bridge, port } = await startBridge();
  t.after(() => bridge.close());
  const denied = await get(port, '/');
  assert.equal(denied.status, 401);
});

test('the pet page and its character asset are served with the token', async t => {
  const { bridge, port } = await startBridge();
  t.after(() => bridge.close());
  const token = bridge.token;
  const page = await get(port, `/?t=${token}`);
  assert.equal(page.status, 200);
  assert.match(page.body, /陪玩桌宠/);
  assert.match(page.headers['content-type'], /text\/html/);

  const asset = await get(port, `/assets/pet-mobile.png?t=${token}`);
  assert.equal(asset.status, 200);
  assert.equal(asset.headers['content-type'], 'image/png');

  const traversal = await get(port, `/assets/..%2Fpackage.json?t=${token}`);
  assert.ok([403, 404].includes(traversal.status), 'path traversal must not serve repository files');
});

test('the event stream replays the current bubble and then streams new ones', async t => {
  const { bridge, port } = await startBridge();
  t.after(() => bridge.close());
  bridge.send({ type: 'reply', lines: ['先出多兰剑'], holdMs: 3000 });

  const received = [];
  const req = request({ host: '127.0.0.1', port, path: `/events?t=${bridge.token}` }, res => {
    res.setEncoding('utf8');
    res.on('data', chunk => {
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data: ')) received.push(JSON.parse(line.slice(6)));
      }
    });
  });
  req.end();
  await once(req, 'response');
  for (let i = 0; i < 40 && !received.length; i++) await new Promise(r => setTimeout(r, 25));
  assert.equal(received[0].type, 'reply');
  assert.deepEqual(received[0].lines, ['先出多兰剑']);

  bridge.send({ type: 'status', text: '正在接收 LOL 数据' });
  for (let i = 0; i < 40 && received.length < 2; i++) await new Promise(r => setTimeout(r, 25));
  assert.equal(received.at(-1).type, 'status');
  req.destroy();
});

test('a phone can post a test line and every client sees it', async t => {
  const { bridge, port } = await startBridge();
  t.after(() => bridge.close());
  const body = JSON.stringify({ text: '测试气泡' });
  const response = await new Promise((resolve, reject) => {
    const req = request({
      host: '127.0.0.1', port, path: `/say?t=${bridge.token}`, method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) },
    }, res => { res.resume(); resolve(res.statusCode); });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
  assert.equal(response, 200);
  assert.equal(bridge.status().clients, 0);
});

test('an occupied port is reported instead of crashing', async t => {
  const first = await startBridge();
  t.after(() => first.bridge.close());
  const second = createMobileBridge({ root, port: first.port, host: '127.0.0.1' });
  second.start();
  await new Promise(r => setTimeout(r, 120));
  assert.equal(second.status().running, false);
  assert.equal(second.status().lastError, 'PORT_IN_USE');
});
