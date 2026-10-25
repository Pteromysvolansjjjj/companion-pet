import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as sleep } from 'node:timers/promises';
import path from 'node:path';
import { createLeagueConnector, createGameWatcher, detectUpdate, sanitizeData, parseLockfile, parseClientCommandLine, parseBootstrapUrl, createCredentialDiscovery, createHttpsTransport, LCU_ENDPOINTS, LIVE_ENDPOINT } from '../../src/game/index.mjs';

function snapshot({ events = [], time = 10, phase = 'InProgress', gameId, live = true, champion = 'Ahri', timer = 20 } = {}) {
  return {
    capturedAt: new Date().toISOString(), self: { status: 'matched', player: { summonerName: 'Me' } },
    sections: {
      phase: { status: 'ok', data: phase },
      champSelect: { status: 'ok', data: { actions: [{ champion }], timer: { phase: 'FINALIZATION', adjustedTimeLeftInPhase: timer } } },
      gameflow: { status: 'ok', data: { gameData: { ...(gameId ? { gameId } : {}) } } },
      live: live ? { status: 'ok', data: { gameData: { gameTime: time }, activePlayer: { summonerName: 'Me', championStats: { currentHealth: 100 - time } }, allPlayers: [{ summonerName: 'Me', scores: { kills: 0 } }], events: { Events: events } } }
        : { status: 'error', reason: 'not_listening' },
    },
  };
}

test('credential parsing accepts valid lockfile/quoted command lines and rejects unsafe protocol', () => {
  assert.deepEqual(parseLockfile('LeagueClient:42:54321:private:https\n'), { port: 54321, password: 'private', pid: 42, source: 'lockfile' });
  assert.equal(parseLockfile('LeagueClient:42:54321:private:http'), null);
  assert.equal(parseLockfile('LeagueClient:42:99999:private:https'), null);
  assert.equal(parseClientCommandLine('LeagueClientUx.exe "--app-port=54321" "--remoting-auth-token=abc_123"').password, 'abc_123');
  assert.equal(parseClientCommandLine('LeagueClientUx.exe --app-port="54321" --remoting-auth-token="abc_123"').port, 54321);
});

test('discovery checks both install-root and LeagueClient lockfiles and invalidates cache', async () => {
  const paths = [];
  let reads = 0;
  const discovery = createCredentialDiscovery({
    installRoots: ['X:/Game'], discoverProcess: false,
    fs: {
      stat: async filename => { paths.push(filename); if (!filename.includes('LeagueClient')) throw new Error('missing'); return { isFile: () => true, size: 40 }; },
      readFile: async () => { reads++; return 'LeagueClient:1:50100:secret:https'; },
      readdir: async () => [],
    },
  });
  assert.equal((await discovery.discover()).port, 50100);
  assert.equal(paths.length, 2);
  await discovery.discover(); assert.equal(reads, 1);
  discovery.invalidate(); await discovery.discover(); assert.equal(reads, 2);
});

test('bootstrap URLs require literal local HTTPS, riot username, an explicit port and exact bootstrap path', () => {
  assert.deepEqual(parseBootstrapUrl('bootstrap="https://riot:test_secret@127.0.0.1:45678/bootstrap.html"'), { port: 45678, password: 'test_secret', source: 'recent_log' });
  assert.equal(parseBootstrapUrl('https://riot:test%2Bsecret@localhost:45678/bootstrap.html?region=test').password, 'test+secret');
  for (const url of [
    'http://riot:test@localhost:45678/bootstrap.html',
    'https://other:test@localhost:45678/bootstrap.html',
    'https://riot:test@example.com:45678/bootstrap.html',
    'https://riot:test@127.0.0.1.evil.test:45678/bootstrap.html',
    'https://riot:test@2130706433:45678/bootstrap.html',
    'https://riot:test@127.0.0.2:45678/bootstrap.html',
    'https://riot:test@localhost/bootstrap.html',
    'https://riot:test@localhost:70000/bootstrap.html',
    'https://riot:test@localhost:45678/other.html',
    'https://riot:test%0Asecret@localhost:45678/bootstrap.html',
  ]) assert.equal(parseBootstrapUrl(url), null, url);
});

test('recent bootstrap discovery only reads newest three files from known log locations, never exposes token', async () => {
  const root = path.join('X:', 'Game');
  const expectedDirectories = [path.join(root, 'Logs', 'LeagueClient Logs'), path.join(root, 'LeagueClient', 'Logs', 'LeagueClient Logs'), path.join('X:', 'ExtraLogs')];
  const now = Date.now();
  const logs = new Map([
    [path.join(expectedDirectories[0], '01_LeagueClientUx.log'), { age: 0, content: 'no credentials' }],
    [path.join(expectedDirectories[0], '02_LeagueClientUx.log'), { age: 1000, content: 'no credentials' }],
    [path.join(expectedDirectories[1], '03_LeagueClientUx.log'), { age: 2000, content: 'no credentials' }],
    [path.join(expectedDirectories[2], '04_LeagueClientUx.log'), { age: 3000, content: 'https://riot:DO_NOT_EXPOSE@localhost:45555/bootstrap.html' }],
    [path.join(expectedDirectories[0], 'old_LeagueClientUx.log'), { age: 3 * 60 * 60 * 1000, content: 'https://riot:OLD_SECRET@localhost:45555/bootstrap.html' }],
  ]);
  const listed = [];
  const read = [];
  const fs = {
    async stat(filename) { const log = logs.get(filename); if (!log) throw new Error('missing'); return { isFile: () => true, size: log.content.length, mtimeMs: now - log.age }; },
    async readdir(directory) {
      listed.push(directory);
      return [...logs.keys()].filter(filename => path.dirname(filename) === directory).map(filename => ({ name: path.basename(filename), isFile: () => true }));
    },
    async readFile(filename) { read.push(filename); return logs.get(filename).content; },
  };
  const discovery = createCredentialDiscovery({ installRoots: [root], logDirectories: [expectedDirectories[2]], discoverProcess: false, fs });
  assert.equal(await discovery.discover(), null);
  assert.equal(read.length, 3);
  assert.deepEqual(new Set(listed), new Set([...expectedDirectories, root, path.join(root, 'LeagueClient')]));
  logs.get(path.join(expectedDirectories[1], '03_LeagueClientUx.log')).content = 'https://riot:DO_NOT_EXPOSE@127.0.0.1:45555/bootstrap.html';
  discovery.invalidate();
  const credential = await discovery.discover();
  assert.equal(credential.password, 'DO_NOT_EXPOSE');
  const connector = createLeagueConnector({ discovery, transport: { request: async () => ({ status: 404 }) } });
  const publicSnapshot = await connector.read();
  assert.equal(JSON.stringify(publicSnapshot).includes('DO_NOT_EXPOSE'), false);
});

test('regional direct-root logs can belong to a still-running protected process older than two hours', async () => {
  const root = path.join('X:', 'Game');
  const activeLog = path.join(root, 'LeagueClient', '2026-10-24T12-30-50_30672_37084_LeagueClientUx.log');
  const helperLog = path.join(root, '2026-10-24T12-30-50_30672_37084_LeagueClientUxHelper.log');
  const staleLog = path.join(root, '2026-10-24T12-30-50_30672_37085_LeagueClientUx.log');
  const oversizedLog = path.join(root, '2026-10-24T12-30-50_30672_37084_LeagueClientUx.log');
  const files = new Map([
    [activeLog, { mtimeMs: Date.now() - 4 * 60 * 60 * 1000, size: 120 }],
    [helperLog, { mtimeMs: Date.now(), size: 120 }],
    [staleLog, { mtimeMs: Date.now() - 3 * 60 * 60 * 1000, size: 120 }],
    [oversizedLog, { mtimeMs: Date.now(), size: 9 * 1024 * 1024 }],
  ]);
  const read = [];
  const discovery = createCredentialDiscovery({
    installRoots: [root], processQuery: async () => [{ ProcessId: 37084, ExecutablePath: null, CommandLine: null }],
    fs: {
      async stat(filename) { if (!files.has(filename)) throw new Error('missing'); return { isFile: () => true, ...files.get(filename) }; },
      async readdir(directory) { return [...files.keys()].filter(filename => path.dirname(filename) === directory).map(filename => ({ name: path.basename(filename), isFile: () => true })); },
      async readFile(filename) { read.push(filename); return 'https://riot:TEST_ONLY@127.0.0.1:45555/bootstrap.html'; },
    },
  });
  assert.equal((await discovery.discover()).port, 45555);
  assert.deepEqual(read, [activeLog]);
});

test('sanitization recursively removes credentials and voice data but retains game facts', () => {
  assert.deepEqual(sanitizeData({ score: 1, nested: [{ Password: 'a', authToken: 'b', championName: 'Ahri', voiceChat: { password: 'x' }, authorization: 'c' }] }), { score: 1, nested: [{ championName: 'Ahri' }] });
});

test('sanitization preserves JSON prototype-like keys as data without changing ordinary object prototypes', () => {
  const value = JSON.parse('{"__proto__":{"marker":"game-data","authToken":"hidden"},"constructor":{"prototype":{"stat":7}},"players":[{"__proto__":{"hp":100},"name":"Me"}]}');
  const clean = sanitizeData(value);
  assert.equal(Object.getPrototypeOf(clean), Object.prototype);
  assert.equal(Object.getPrototypeOf(clean.players[0]), Object.prototype);
  assert.equal(Object.hasOwn(clean, '__proto__'), true);
  assert.equal(Object.hasOwn(clean.players[0], '__proto__'), true);
  assert.equal(clean.marker, undefined);
  assert.equal(clean.players[0].hp, undefined);
  assert.deepEqual(clean.__proto__, { marker: 'game-data' });
  assert.deepEqual(clean.players[0].__proto__, { hp: 100 });
  assert.deepEqual(clean.constructor, { prototype: { stat: 7 } });
  assert.equal(JSON.stringify(clean).includes('hidden'), false);
  assert.deepEqual(JSON.parse(JSON.stringify(clean)), clean);
});

test('connector only requests approved GET paths, sanitizes snapshot and matches self', async () => {
  const calls = [];
  const connector = createLeagueConnector({
    discovery: { discover: async () => ({ port: 50000, password: 'private' }) },
    transport: { request: async args => {
      calls.push(args);
      if (args.path === LIVE_ENDPOINT) return { status: 200, data: { activePlayer: { riotId: 'Me#ONE' }, allPlayers: [{ riotId: 'Me#ONE', championName: 'Ahri' }], events: { Events: [] } } };
      if (args.path === LCU_ENDPOINTS.phase) return { status: 200, data: 'Lobby' };
      return { status: 200, data: { token: 'private', playerLevel: 123 } };
    } },
  });
  const result = await connector.read();
  assert.equal(calls.length, Object.keys(LCU_ENDPOINTS).length + 1);
  assert.equal(result.sources.lcu.available, true);
  assert.equal(result.self.status, 'matched');
  assert.equal(JSON.stringify(result).includes('private'), false);
  assert.equal(calls.at(-1).password, undefined);
});

test('authentication failure invalidates credentials and never passes upstream error text', async () => {
  let invalidated = false;
  const connector = createLeagueConnector({
    discovery: { discover: async () => ({ port: 50000, password: 'SECRET' }), invalidate() { invalidated = true; } },
    transport: { request: async ({ port }) => port === 2999 ? { status: 404 } : { status: 401, data: { error: 'SECRET' } } },
  });
  const result = await connector.read();
  assert.equal(invalidated, true);
  assert.equal(result.sections.phase.reason, 'authentication_failed');
  assert.equal(JSON.stringify(result).includes('SECRET'), false);
});

test('connector serializes simultaneous snapshots without overlapping network calls', async () => {
  let active = 0, peak = 0;
  const connector = createLeagueConnector({
    discovery: { discover: async () => null },
    transport: { request: async () => { active++; peak = Math.max(peak, active); await sleep(10); active--; return { status: 404 }; } },
  });
  await Promise.all([connector.read(), connector.read(), connector.read()]);
  assert.equal(peak, 1);
});

test('transport rejects any nonallowlisted path before a network request', async () => {
  await assert.rejects(createHttpsTransport().request({ port: 2999, path: '/evil' }), /not permitted/);
});

test('event baseline never replays existing history; repeated event IDs are not assumed to start at zero', () => {
  const a = { EventID: 47, EventTime: 10, EventName: 'ChampionKill', VictimName: 'Me' };
  const b = { EventID: 52, EventTime: 11, EventName: 'ChampionKill', VictimName: 'Other' };
  const first = detectUpdate(snapshot({ events: [a] }), undefined, { dataMode: 'detailed' });
  assert.deepEqual(first.update.events, []);
  const second = detectUpdate(snapshot({ events: [a, b], time: 11 }), first.next, { dataMode: 'detailed' });
  assert.deepEqual(second.update.events, [b]);
  const third = detectUpdate(snapshot({ events: [a, b], time: 12 }), second.next, { dataMode: 'detailed' });
  assert.deepEqual(third.update.events, []);
  assert.equal(third.update.observation.sections.live.data.gameData.gameTime, 12);
  assert.equal(detectUpdate(snapshot({ events: [a, b], time: 12 }), third.next, { dataMode: 'detailed' }).update, null);
});

test('temporary disconnection retains cursor and only delivers new events on reconnect', () => {
  const a = { EventID: 17, EventTime: 10, EventName: 'GameStart' };
  const b = { EventID: 18, EventTime: 13, EventName: 'ChampionKill' };
  const first = detectUpdate(snapshot({ events: [a] }));
  const outage = detectUpdate(snapshot({ live: false }), first.next);
  const reconnect = detectUpdate(snapshot({ events: [a, b], time: 14 }), outage.next);
  assert.deepEqual(reconnect.update.events, [b]);
  assert.equal(reconnect.update.sessionChange, undefined);
});

test('new game without gameId is detected from clock rollback and includes its GameStart event', () => {
  const old = detectUpdate(snapshot({ events: [{ EventID: 9, EventTime: 100, EventName: 'ChampionKill' }], time: 800 }));
  const fresh = detectUpdate(snapshot({ events: [{ EventID: 9, EventTime: 1, EventName: 'GameStart' }], time: 5 }), old.next);
  assert.equal(fresh.update.sessionChange, 'game_clock_restarted');
  assert.deepEqual(fresh.update.events, [{ EventID: 9, EventTime: 1, EventName: 'GameStart' }]);
  const kill = { EventID: 10, EventTime: 6, EventName: 'ChampionKill' };
  const after = detectUpdate(snapshot({ events: [kill], time: 6 }), fresh.next);
  assert.deepEqual(after.update.events, [kill]);
});

test('endgame then lobby then in-progress detects a new game even without an ID or clock rollback', () => {
  const first = detectUpdate(snapshot({ time: 5 }));
  const ended = detectUpdate(snapshot({ live: false, phase: 'EndOfGame' }), first.next);
  const lobby = detectUpdate(snapshot({ live: false, phase: 'Lobby' }), ended.next);
  const next = detectUpdate(snapshot({ phase: 'InProgress', time: 10, events: [{ EventID: 0, EventName: 'GameStart' }] }), lobby.next);
  assert.equal(next.update.sessionChange, 'client_entered_new_game');
  assert.equal(next.update.events.length, 1);
});

test('watching from lobby includes the first new game event instead of treating it as old history', () => {
  const lobby = detectUpdate(snapshot({ phase: 'Lobby', live: false }));
  const selecting = detectUpdate(snapshot({ phase: 'ChampSelect', live: false }), lobby.next);
  const start = detectUpdate(snapshot({ phase: 'InProgress', time: 1, events: [{ EventID: 4, EventName: 'GameStart' }] }), selecting.next);
  assert.equal(start.update.sessionChange, 'client_entered_new_game');
  assert.deepEqual(start.update.events, [{ EventID: 4, EventName: 'GameStart' }]);
});

test('LCU independent calls are bounded while complete concurrent reads stay serialized', async () => {
  let active = 0, peak = 0;
  const connector = createLeagueConnector({
    discovery: { discover: async () => ({ port: 50000, password: 'private' }) },
    transport: { request: async () => { active++; peak = Math.max(peak, active); await sleep(5); active--; return { status: 200, data: {} }; } },
  });
  await Promise.all([connector.read(), connector.read()]);
  assert.equal(peak, 3);
});

test('new game uses optional client game ID even if a long outage hides clock rollback', () => {
  const old = detectUpdate(snapshot({ gameId: 123, time: 100 }));
  const fresh = detectUpdate(snapshot({ gameId: 456, time: 200 }), old.next);
  assert.equal(fresh.update.sessionChange, 'client_game_id_changed');
});

test('timer, health and selection changes produce full observations without semantic filtering', () => {
  const first = detectUpdate(snapshot({ time: 10, timer: 20 }), undefined, { dataMode: 'detailed' });
  const continuous = detectUpdate(snapshot({ time: 11, timer: 19 }), first.next, { dataMode: 'detailed' });
  assert.equal(continuous.update.observation.sections.live.data.activePlayer.championStats.currentHealth, 89);
  assert.equal(continuous.update.observation.sections.champSelect.data.timer.adjustedTimeLeftInPhase, 19);
  const selection = detectUpdate(snapshot({ champion: 'Lux', timer: 18 }), first.next, { dataMode: 'detailed' });
  assert.equal(selection.update.observation.sections.champSelect.data.actions[0].champion, 'Lux');
});

test('delivery failure retries the same update ID before collecting later changes', async () => {
  let reads = 0;
  const attempts = [];
  const watcher = createGameWatcher({ intervalMs: 50, connector: { read: async () => { reads++; return snapshot(); } },
    onUpdate: async update => { attempts.push(update.id); if (attempts.length === 1) throw new Error('temporary delivery failure'); } });
  watcher.start();
  await sleep(575);
  await watcher.stop();
  assert.ok(attempts.length >= 2);
  assert.equal(attempts[0], attempts[1]);
  assert.equal(watcher.status().pendingDelivery, false);
  assert.ok(reads <= 3);
});

test('stop cancels an in-flight read and prevents subsequent delivery', async () => {
  let deliveries = 0;
  const watcher = createGameWatcher({ connector: { read: async ({ signal }) => {
    await sleep(1000, null, { signal }); return snapshot();
  } }, onUpdate: async () => { deliveries++; } });
  watcher.start(); await sleep(10); await watcher.stop();
  assert.equal(deliveries, 0);
  assert.equal(watcher.status().running, false);
});

test('stop does not hang on a host delivery promise and passes an abort signal', async () => {
  let deliveredSignal;
  const watcher = createGameWatcher({ connector: { read: async () => snapshot() }, onUpdate: (_update, { signal }) => { deliveredSignal = signal; return new Promise(() => {}); } });
  watcher.start(); await sleep(10); await watcher.stop();
  assert.equal(deliveredSignal.aborted, true);
  assert.equal(watcher.status().running, false);
});
