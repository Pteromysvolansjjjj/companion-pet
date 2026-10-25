import test from 'node:test';
import assert from 'node:assert/strict';
import { detectUpdate, projectObservation, createGameWatcher } from '../../src/game/index.mjs';

function fixture() {
  const account = { summonerId: 123, puuid: 'self-id', gameName: 'Me', tagLine: 'ONE', summonerLevel: 470, xpSinceLastLevel: 100 };
  const activePlayer = { riotId: 'Me#ONE', level: 11, championStats: { currentHealth: 500 }, currentGold: 820, abilities: { Q: { level: 3 } } };
  const player = { riotId: 'Me#ONE', championName: 'Aurora', level: 11, team: 'ORDER', position: 'MIDDLE', isDead: false, respawnTimer: 0, scores: { kills: 3, deaths: 8, assists: 25, creepScore: 100 }, items: [{ itemID: 1001 }] };
  return {
    capturedAt: 't1', sources: { lcu: { available: true }, live: { available: true } },
    self: { status: 'matched', account, player, activePlayer },
    sections: {
      phase: { status: 'ok', data: 'InProgress' },
      summoner: { status: 'ok', data: account },
      lobby: { status: 'ok', data: { gameConfig: { queueId: 2400, mapId: 12, gameMode: 'KIWI', customGameName: 'My lobby', metadata: 'omit' }, members: [{ summonerId: 123, isLeader: true, firstPositionPreference: 'MIDDLE' }], localMember: { summonerId: 123, isLeader: true } } },
      matchmaking: { status: 'ok', data: { searchState: 'Searching', isCurrentlyInQueue: true, queueId: 2400, timeInQueue: 10 } },
      readyCheck: { status: 'ok', data: { state: 'InProgress', playerResponse: 'Accepted', timer: 9 } },
      champSelect: { status: 'ok', data: { localPlayerCellId: 2, myTeam: [{ cellId: 2, summonerId: 123, championId: 893, championPickIntent: 893, assignedPosition: 'MIDDLE' }], theirTeam: [], actions: [[{ id: 1, actorCellId: 2, type: 'pick', championId: 893, completed: true, isInProgress: false }]], bans: { myTeamBans: [103], theirTeamBans: [] }, timer: { phase: 'FINALIZATION', adjustedTimeLeftInPhase: 5000 } } },
      gameflow: { status: 'ok', data: { phase: 'InProgress', gameData: { gameId: 1, queue: { id: 2400, gameMode: 'KIWI', mapId: 12, name: 'Arena variation' } }, map: { id: 12, name: 'Howling Abyss' }, gameClient: { serverIp: 'omit' } } },
      live: { status: 'ok', data: { activePlayer, allPlayers: [player], events: { Events: [] }, gameData: { gameTime: 500, gameMode: 'KIWI', mapNumber: 12, mapName: 'Howling Abyss' } } },
      endgame: { status: 'unavailable', reason: 'not_available_in_current_state' },
    },
  };
}

test('basic projection keeps explicit identity/mode/selection/scoreboard fields and removes detailed aliases', () => {
  const raw = fixture();
  const basic = projectObservation(raw, 'basic');
  assert.equal(basic.dataMode, 'basic');
  assert.equal(basic.sections.phase.data, 'InProgress');
  assert.equal(basic.sections.summoner.data.summonerLevel, 470);
  assert.equal(basic.sections.lobby.data.gameConfig.queueId, 2400);
  assert.equal(basic.sections.champSelect.data.actions[0][0].championId, 893);
  assert.equal(basic.sections.champSelect.data.timer.phase, 'FINALIZATION');
  assert.equal(basic.sections.gameflow.data.gameData.queue.gameMode, 'KIWI');
  assert.equal(basic.sections.live.data.allPlayers[0].isDead, false);
  assert.deepEqual(basic.sections.live.data.allPlayers[0].scores, { kills: 3, deaths: 8, assists: 25 });
  assert.deepEqual(basic.sections.endgame, raw.sections.endgame);
  for (const active of [basic.self.activePlayer, basic.sections.live.data.activePlayer]) {
    assert.equal(Object.hasOwn(active, 'championStats'), false);
    assert.equal(Object.hasOwn(active, 'currentGold'), false);
    assert.equal(Object.hasOwn(active, 'abilities'), false);
  }
  for (const player of [basic.self.player, basic.sections.live.data.allPlayers[0]]) {
    assert.equal(Object.hasOwn(player, 'items'), false);
    assert.equal(Object.hasOwn(player, 'respawnTimer'), false);
    assert.equal(Object.hasOwn(player.scores, 'creepScore'), false);
  }
  assert.equal(Object.hasOwn(basic.self.account, 'xpSinceLastLevel'), false);
  assert.equal(Object.hasOwn(basic.sections.live.data.gameData, 'gameTime'), false);
  assert.equal(Object.hasOwn(basic.sections.champSelect.data.timer, 'adjustedTimeLeftInPhase'), false);
  assert.deepEqual(projectObservation(raw), basic, 'the default is the economical basic preset');
  assert.equal(createGameWatcher({ connector: { read() {} }, onUpdate() {} }).status().dataMode, 'basic');
  assert.equal(Object.hasOwn(raw, 'dataMode'), false, 'projection must not mutate the full queryable snapshot');
});

test('basic health/gold/timer-only changes do not trigger while every new event and selected-field change does', () => {
  const first = detectUpdate(fixture(), undefined, { dataMode: 'basic' });
  const next = fixture();
  next.capturedAt = 't2';
  next.sections.live.data.activePlayer.championStats.currentHealth = 20;
  next.sections.live.data.activePlayer.currentGold = 900;
  next.sections.live.data.gameData.gameTime = 501;
  next.sections.champSelect.data.timer.adjustedTimeLeftInPhase = 4000;
  next.sections.matchmaking.data.timeInQueue = 11;
  assert.equal(detectUpdate(next, first.next, { dataMode: 'basic' }).update, null);
  const event = { EventID: 7, EventTime: 501, EventName: 'ChampionKill', VictimName: 'Me', customMetadata: { preserved: true } };
  next.sections.live.data.events.Events.push(event);
  const detected = detectUpdate(next, first.next, { dataMode: 'basic' });
  assert.deepEqual(detected.update.events, [event]);
  assert.deepEqual(detected.update.observation.sections.live.data.events, { eventCount: 1, lastEventId: 7, lastEventTime: 501, logIncluded: false });
  for (const mutate of [
    state => state.sections.live.data.allPlayers[0].scores.kills++,
    state => state.sections.live.data.allPlayers[0].level++,
    state => state.sections.live.data.allPlayers[0].isDead = true,
    state => state.sections.summoner.data.summonerLevel++,
    state => state.sections.champSelect.data.myTeam[0].championId++,
    state => state.sections.lobby.data.gameConfig.queueId++,
  ]) {
    const changed = fixture(); mutate(changed);
    assert.ok(detectUpdate(changed, first.next, { dataMode: 'basic' }).update);
  }
});

test('basic mode still detects raw game-clock rollback and does not replay old events on mode changes', () => {
  const raw = fixture();
  const event = { EventID: 7, EventTime: 1, EventName: 'GameStart' };
  raw.sections.live.data.events.Events = [event];
  const first = detectUpdate(raw, undefined, { dataMode: 'basic' });
  assert.deepEqual(first.update.events, []);
  const detailed = detectUpdate(raw, first.next, { dataMode: 'detailed' });
  assert.equal(detailed.update.observation.dataMode, 'detailed');
  assert.deepEqual(detailed.update.events, []);
  const basic = detectUpdate(raw, detailed.next, { dataMode: 'basic' });
  assert.deepEqual(basic.update.events, []);
  raw.sections.live.data.gameData.gameTime = 1;
  const restarted = detectUpdate(raw, basic.next, { dataMode: 'basic' });
  assert.equal(restarted.update.sessionChange, 'game_clock_restarted');
  assert.deepEqual(restarted.update.events, [event]);
});

test('unknown data modes reject instead of silently choosing a data volume', () => {
  for (const dataMode of ['all', '', null, 0]) {
    assert.throws(() => projectObservation(fixture(), dataMode), /dataMode/);
    assert.throws(() => detectUpdate(fixture(), undefined, { dataMode }), /dataMode/);
    assert.throws(() => createGameWatcher({ dataMode, connector: { read() {} }, onUpdate() {} }), /dataMode/);
  }
});

test('both modes omit cumulative event bodies, retain honest metadata and leave direct-query data intact', () => {
  const raw = fixture();
  const old = { EventID: 3, EventTime: 20, EventName: 'ChampionKill', KillerName: 'event-only-name', detail: { message: 'event-only-message' } };
  raw.sections.live.data.events = {
    Events: [old], schemaVersion: 2, extra: { source: 'live-api' },
    eventCount: 999, lastEventId: 'stale', lastEventTime: 'stale', logIncluded: true,
  };
  const saved = structuredClone(raw);
  for (const mode of ['basic', 'detailed']) {
    const observation = projectObservation(raw, mode);
    assert.deepEqual(observation.sections.live.data.events, {
      schemaVersion: 2, extra: { source: 'live-api' }, eventCount: 1, lastEventId: 3, lastEventTime: 20, logIncluded: false,
    });
    assert.equal(JSON.stringify(observation).includes('event-only-'), false);
    observation.sections.live.data.events.extra.source = 'changed';
    assert.deepEqual(raw, saved);
    const initial = detectUpdate(raw, undefined, { dataMode: mode });
    assert.deepEqual(initial.update.events, [], 'midgame attachment must not replay old bodies');
    assert.equal(initial.update.observation.sections.live.data.events.eventCount, 1);
  }
});

test('empty or missing event history does not retain stale summary values or invent missing identifiers', () => {
  for (const mode of ['basic', 'detailed']) {
    for (const [events, expected] of [
      [{ Events: [], lastEventId: 9, lastEventTime: 20 }, { eventCount: 0, logIncluded: false }],
      [{ schemaVersion: 2, eventCount: 99, lastEventId: 9 }, { schemaVersion: 2, logIncluded: false }],
      [{ Events: [{ EventName: 'GameStart' }] }, { eventCount: 1, logIncluded: false }],
      [{ Events: [{ EventID: 0, EventTime: 0 }] }, { eventCount: 1, lastEventId: 0, lastEventTime: 0, logIncluded: false }],
    ]) {
      const raw = fixture(); raw.sections.live.data.events = events;
      assert.deepEqual(projectObservation(raw, mode).sections.live.data.events, expected);
    }
  }
});
