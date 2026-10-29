import test from 'node:test';
import assert from 'node:assert/strict';
import { createScreenConnector } from '../../src/vision/connector.mjs';
import { projectObservation } from '../../src/game/projection.mjs';
import { detectUpdate } from '../../src/game/watcher.mjs';

const bridge = frame => ({
  latestFrame: () => frame,
  status: () => ({ frames: frame ? 1 : 0 }),
});

function frame(overrides = {}) {
  return {
    type: 'frame', id: 1, capturedAt: new Date().toISOString(),
    window: { title: 'League of Legends (TM) Client', width: 1920, height: 1080, foreground: true },
    durationMs: 420, regions: { top: 12 },
    fields: { gameTime: 600, allyKills: 5, enemyKills: 3, level: 8 },
    bars: { hp: 0.52 },
    board: { rows: [{ kills: 2, deaths: 1, assists: 4, cs: 120, cells: ['亚索'] }] },
    lines: [
      { text: '10:00', region: 'top', score: 0.9 },
      { text: '亚索 击杀了 劫', region: 'feed', score: 0.7 },
      { text: '10:00', region: 'top', score: 0.8 },
    ],
    events: [{ EventID: 1, EventName: 'ChampionKill', EventTime: 600, KillerName: '亚索', VictimName: '劫' }],
    ...overrides,
  };
}

test('screen frame becomes the same snapshot shape the interface connector returns', () => {
  const connector = createScreenConnector({ bridge: bridge(frame()) });
  const snapshot = connector.read();
  assert.equal(snapshot.sections.phase.data, 'InProgress');
  assert.equal(snapshot.sections.screen.status, 'ok');
  assert.equal(snapshot.sections.screen.data.source, 'screen_ocr');
  assert.equal(snapshot.sections.screen.data.fields.gameTime, 600);
  assert.equal(snapshot.sections.screen.data.fields.enemyKills, 3);
  assert.equal(snapshot.sections.live.data.gameData.gameTime, 600);
  assert.equal(snapshot.sections.live.data.activePlayer.level, 8);
  assert.equal(snapshot.sections.live.data.activePlayer.healthRatio, 0.52);
  assert.equal(snapshot.sections.live.data.allPlayers[0].minionsKilled, 120);
  assert.deepEqual(snapshot.sections.live.data.events.Events.map(event => event.EventID), [1]);
  assert.equal(snapshot.sources.screen.available, true);
  assert.equal(snapshot.sources.lcu.available, false);
  assert.equal(snapshot.sections.screen.data.lines.length, 2, 'identical OCR lines are collapsed');
});

test('events are delivered once per frame, not once per read', () => {
  const current = frame();
  const connector = createScreenConnector({ bridge: bridge(current) });
  assert.equal(connector.read().sections.live.data.events.Events.length, 1);
  assert.equal(connector.read().sections.live.data.events.Events.length, 0, 'the same frame is not re-delivered');
  current.id = 2;
  current.events = [{ EventID: 2, EventName: 'ChampionKill', EventTime: 605, KillerName: '李青', VictimName: '阿狸' }];
  assert.deepEqual(connector.read().sections.live.data.events.Events.map(event => event.EventID), [2]);
});

test('a stale or missing screen produces a stable unavailable observation', () => {
  const old = frame({ capturedAt: new Date(Date.now() - 10_000).toISOString() });
  const stale = createScreenConnector({ bridge: bridge(old) });
  const first = stale.read();
  const second = stale.read();
  assert.equal(first.sections.live.status, 'unavailable');
  assert.equal(first.sections.live.reason, 'screen_stale');
  assert.deepEqual(first, second, 'no churn while the game is not in front');

  const missing = createScreenConnector({ bridge: bridge(null) }).read();
  assert.equal(missing.sections.screen.reason, 'no_screen_frame');
  assert.equal(missing.sections.phase.status, 'unavailable');
});

test('the existing watcher and projection accept screen snapshots unchanged', () => {
  const current = frame();
  const connector = createScreenConnector({ bridge: bridge(current) });
  const first = detectUpdate(connector.read(), undefined, { dataMode: 'basic' });
  assert.equal(first.update.initial, true);
  assert.equal(first.update.events.length, 0, 'the first screen snapshot is a baseline, like the interface path');
  assert.equal(first.update.observation.sections.screen.data.fields.gameTime, 600);
  assert.equal(first.update.observation.sections.live.data.gameData.gameTime, undefined,
    'the economical basic preset still omits the clock from live.gameData; screen.fields carries it');

  current.id = 2;
  current.fields = { ...current.fields, gameTime: 605 };
  current.events = [{ EventID: 9, EventName: 'ChampionKill', EventTime: 605, KillerName: '李青', VictimName: '阿狸' }];
  const second = detectUpdate(connector.read(), first.next, { dataMode: 'basic' });
  assert.equal(second.update.events.length, 1, 'a kill that appears later is delivered');
  assert.equal(second.update.events[0].KillerName, '李青');

  const projected = projectObservation(connector.read(), 'basic');
  assert.equal(projected.sections.screen.data.source, 'screen_ocr');
  assert.equal(projected.sections.live.data.activePlayer.level, 8);
});
