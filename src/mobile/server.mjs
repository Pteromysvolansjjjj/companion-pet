/**
 * Local HTTP server that puts the companion on a phone.
 *
 * The phone is a second screen: it receives the same bubbles the desktop pet
 * shows, plus a compact status line.  It is not a chat client, holds no model
 * access and never receives game payloads — only the text that was already
 * displayed on the desktop.
 *
 * The server binds to the LAN so a phone can reach it; every request must carry
 * the per-start token that is printed in the plugin log.
 */

import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { networkInterfaces } from 'node:os';
import { extname, join, normalize } from 'node:path';
import { randomBytes, timingSafeEqual } from 'node:crypto';

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
};

function lanAddress() {
  for (const entries of Object.values(networkInterfaces())) {
    for (const entry of entries ?? []) {
      if (entry.family === 'IPv4' && !entry.internal) return entry.address;
    }
  }
  return '127.0.0.1';
}

function safeEqual(a, b) {
  const left = Buffer.from(String(a ?? ''));
  const right = Buffer.from(String(b ?? ''));
  if (left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}

export function createMobileBridge({
  root,
  port = 8790,
  host = '0.0.0.0',
  enabled = true,
  log = () => {},
  keepAliveMs = 25000,
} = {}) {
  const token = randomBytes(9).toString('base64url');
  const clients = new Set();
  const history = [];
  let server = null;
  let heartbeat = null;
  let running = false;
  let boundPort = port;
  let lastError = null;
  let served = 0;

  const pagePath = join(root, 'client', 'pet.html');
  const assetRoot = join(root, 'assets');

  function remember(message) {
    history.push(message);
    if (history.length > 20) history.shift();
  }

  function broadcast(message) {
    const payload = `data: ${JSON.stringify(message)}\n\n`;
    for (const response of clients) {
      try { response.write(payload); } catch { clients.delete(response); }
    }
  }

  function send(message) {
    if (!message || typeof message.type !== 'string') return false;
    remember(message);
    if (clients.size) broadcast(message);
    return true;
  }

  function authorised(url) {
    const supplied = url.searchParams.get('t') ?? '';
    return safeEqual(supplied, token);
  }

  function serveFile(response, filePath) {
    if (!existsSync(filePath) || !statSync(filePath).isFile()) {
      response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
      response.end('not found');
      return;
    }
    const type = CONTENT_TYPES[extname(filePath).toLowerCase()] ?? 'application/octet-stream';
    response.writeHead(200, {
      'content-type': type,
      'cache-control': 'no-cache',
      'x-content-type-options': 'nosniff',
    });
    createReadStream(filePath).pipe(response);
  }

  async function handle(request, response) {
    let url;
    try {
      url = new URL(request.url, `http://${request.headers.host ?? 'localhost'}`);
    } catch {
      response.writeHead(400); response.end('bad request'); return;
    }
    if (!authorised(url)) {
      response.writeHead(401, { 'content-type': 'text/plain; charset=utf-8' });
      response.end('unauthorised: open the address printed by the plugin');
      return;
    }

    if (url.pathname === '/' || url.pathname === '/index.html') {
      served++;
      serveFile(response, pagePath);
      return;
    }
    if (url.pathname === '/health') {
      response.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
      response.end(JSON.stringify({ ok: true, clients: clients.size, served, lastError }));
      return;
    }
    if (url.pathname.startsWith('/assets/')) {
      const relative = normalize(url.pathname.slice('/assets/'.length));
      if (relative.startsWith('..')) { response.writeHead(403); response.end(); return; }
      serveFile(response, join(assetRoot, relative));
      return;
    }
    if (url.pathname === '/events') {
      response.writeHead(200, {
        'content-type': 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache, no-transform',
        connection: 'keep-alive',
        'x-accel-buffering': 'no',
      });
      response.write(': connected\n\n');
      clients.add(response);
      // A phone that reconnects should immediately see the current bubble.
      for (const message of history.slice(-6)) {
        try { response.write(`data: ${JSON.stringify(message)}\n\n`); } catch { /* closed */ }
      }
      request.on('close', () => clients.delete(response));
      return;
    }
    if (url.pathname === '/say' && request.method === 'POST') {
      let body = '';
      request.on('data', chunk => {
        body += chunk;
        if (body.length > 4096) request.destroy();
      });
      request.on('end', () => {
        let text = '';
        try { text = String(JSON.parse(body || '{}').text ?? ''); } catch { /* ignore */ }
        if (text) send({ type: 'reply', lines: [text.slice(0, 200)], holdMs: 4000 });
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ ok: Boolean(text) }));
      });
      return;
    }

    response.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    response.end('not found');
  }

  function start() {
    if (!enabled || running) return running;
    if (!existsSync(pagePath)) { lastError = 'PAGE_MISSING'; return false; }
    server = createServer((request, response) => {
      handle(request, response).catch(error => {
        lastError = 'REQUEST_FAILED';
        try { response.writeHead(500); response.end('error'); } catch { /* ignore */ }
        log('mobile request failed', error?.message);
      });
    });
    server.on('error', error => {
      lastError = error?.code === 'EADDRINUSE' ? 'PORT_IN_USE' : 'SERVER_ERROR';
      running = false;
      log('mobile server error', error?.message);
    });
    server.listen(port, host, () => {
      running = true;
      boundPort = server.address()?.port ?? port;
      lastError = null;
      log(`手机桌宠: http://${lanAddress()}:${port}/?t=${token}`);
    });
    heartbeat = setInterval(() => {
      for (const response of clients) {
        try { response.write(': ping\n\n'); } catch { clients.delete(response); }
      }
    }, keepAliveMs);
    heartbeat.unref?.();
    return true;
  }

  function close() {
    running = false;
    if (heartbeat) clearInterval(heartbeat);
    heartbeat = null;
    for (const response of clients) { try { response.end(); } catch { /* ignore */ } }
    clients.clear();
    return new Promise(resolve => {
      if (!server) { resolve(); return; }
      server.close(() => resolve());
      server = null;
    });
  }

  return {
    start,
    close,
    send,
    url: () => `http://${lanAddress()}:${boundPort}/?t=${token}`,
    status: () => ({ enabled, running, clients: clients.size, port: boundPort, url: running ? `http://${lanAddress()}:${boundPort}/?t=${token}` : null, lastError }),
    get token() { return token; },
  };
}
