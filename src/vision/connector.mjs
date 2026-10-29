/**
 * Screen-OCR data source.
 *
 * Produces the *same snapshot shape* the LCU / Live Client connector returns, so
 * the existing watcher, projection, batching and conversation delivery run
 * unchanged.  Only the origin of the facts differs, and that difference is
 * labelled (`source: "screen_ocr"`) instead of being passed off as an API read.
 */

const SOURCE = 'screen_ocr';

const unavailableSource = reason => ({
  lcu: { available: false, reason: 'screen_mode' },
  live: { available: false, reason: 'screen_mode' },
  screen: { available: false, reason },
});

function emptySnapshot(reason) {
  return {
    capturedAt: new Date().toISOString(),
    sources: unavailableSource(reason),
    sections: {
      phase: { status: 'unavailable', reason },
      live: { status: 'unavailable', reason },
      screen: { status: 'unavailable', reason },
    },
    self: { status: 'unresolved' },
  };
}

function dedupeLines(lines, limit = 40) {
  const seen = new Set();
  const result = [];
  for (const line of lines ?? []) {
    const text = typeof line?.text === 'string' ? line.text.trim() : '';
    if (!text || seen.has(text)) continue;
    seen.add(text);
    result.push({ text, region: line.region ?? 'unknown', score: line.score });
    if (result.length >= limit) break;
  }
  return result;
}

function boardPlayers(board) {
  const rows = board?.rows ?? [];
  const players = [];
  for (const row of rows) {
    if (row?.kills === undefined) continue;
    const name = Array.isArray(row.cells) && row.cells.length ? String(row.cells[0]) : String(row.text ?? '');
    players.push({
      championName: name,
      kills: row.kills, deaths: row.deaths, assists: row.assists,
      ...(row.cs !== undefined ? { minionsKilled: row.cs } : {}),
      scores: { kills: row.kills, deaths: row.deaths, assists: row.assists },
    });
  }
  return players;
}

function liveSection(frame, events) {
  const fields = frame.fields ?? {};
  const gameData = {};
  if (Number.isFinite(fields.gameTime)) gameData.gameTime = fields.gameTime;
  gameData.gameMode = 'CLASSIC';
  const activePlayer = { source: SOURCE };
  if (Number.isFinite(fields.level)) activePlayer.level = fields.level;
  if (Number.isFinite(fields.allyKills)) activePlayer.teamKills = fields.allyKills;
  if (Number.isFinite(fields.enemyKills)) activePlayer.enemyTeamKills = fields.enemyKills;
  if (frame.bars?.hp !== undefined) activePlayer.healthRatio = frame.bars.hp;
  if (frame.bars?.mana !== undefined) activePlayer.manaRatio = frame.bars.mana;

  const data = { gameData, activePlayer };
  const players = boardPlayers(frame.board);
  if (players.length) data.allPlayers = players;
  data.events = { Events: events, eventCount: events.length, logIncluded: false };

  // Screen readings are estimates; the flag travels with the data so the model
  // can weight them instead of treating a misread digit as fact.
  data.estimated = true;
  return { status: 'ok', data };
}

function snapshotFromFrame(frame, events) {
  const fields = frame.fields ?? {};
  const lines = dedupeLines(frame.lines);
  const inGame = Number.isFinite(fields.gameTime) || lines.length > 0;

  const screen = {
    source: SOURCE,
    window: frame.window ? { title: frame.window.title, width: frame.window.width, height: frame.window.height } : undefined,
    fields,
    ...(frame.bars && Object.keys(frame.bars).length ? { bars: frame.bars } : {}),
    ...(frame.board ? { board: frame.board } : {}),
    lines,
    ...(frame.durationMs > 2000 ? { degraded: true } : {}),
    note: '屏幕 OCR 读数，可能存在误识别；数值冲突时以更稳定的一侧为准。',
  };

  return {
    capturedAt: frame.capturedAt,
    sources: {
      screen: {
        available: true,
        durationMs: frame.durationMs,
        regions: frame.regions,
        window: frame.window,
      },
      lcu: { available: false, reason: 'screen_mode' },
      live: { available: false, reason: 'screen_mode' },
    },
    sections: {
      phase: { status: 'ok', data: inGame ? 'InProgress' : 'Unknown' },
      live: liveSection(frame, events),
      screen: { status: 'ok', data: screen },
    },
    self: { status: 'unresolved' },
  };
}

export function createScreenConnector({ bridge, staleMs = 4000 } = {}) {
  if (!bridge?.latestFrame) throw new TypeError('a vision bridge is required');
  let consumedFrameId = 0;
  let lastReason = null;

  function read() {
    const frame = bridge.latestFrame();
    if (!frame) {
      lastReason = 'no_screen_frame';
      return emptySnapshot(lastReason);
    }
    const age = Date.now() - Date.parse(frame.capturedAt);
    if (!Number.isFinite(age) || age > staleMs) {
      lastReason = 'screen_stale';
      return emptySnapshot(lastReason);
    }
    let events = [];
    if (frame.id !== consumedFrameId) {
      consumedFrameId = frame.id;
      events = Array.isArray(frame.events) ? frame.events : [];
    }
    lastReason = null;
    return snapshotFromFrame(frame, events);
  }

  return {
    /** Synchronous source: the sidecar already samples; the watcher only reads. */
    read({ signal } = {}) {
      if (signal?.aborted) {
        const error = new Error('aborted');
        error.name = 'AbortError';
        throw error;
      }
      return read();
    },
    get latestSnapshot() { return read(); },
    status() {
      const frame = bridge.latestFrame();
      return {
        source: SOURCE,
        available: Boolean(frame) && lastReason === null,
        reason: lastReason,
        ageMs: frame ? Date.now() - Date.parse(frame.capturedAt) : null,
        frames: bridge.status().frames,
      };
    },
  };
}
