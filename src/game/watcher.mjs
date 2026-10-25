import { randomUUID } from 'node:crypto';
import { sanitizeData, safeReason, checkAbort, aborted } from './sanitize.mjs';
import { canonical, observationKey } from './delta.mjs';
import { projectObservation, validateDataMode } from './projection.mjs';

const keyOf = value => JSON.stringify(canonical(value));
const eventKey = event => event.EventID !== undefined && event.EventID !== null
  ? `id:${String(event.EventID)}:${String(event.EventTime ?? '')}` : `content:${keyOf(event)}`;

function initialState() {
  return { initial: true, observationKey: undefined, eventsInitialized: false, seen: new Set(), gameId: undefined, lastGameTime: undefined, phase: undefined, leftGame: false, epoch: 0 };
}

/** Pure detector, exported to make replay/new-game behavior independently testable. */
export function detectUpdate(snapshot, previous = initialState(), { dataMode = 'basic' } = {}) {
  // Event cursors and new-game detection use raw safe data in either preset.
  // State change detection uses exactly the fields of the user's selected mode.
  snapshot = sanitizeData(snapshot);
  const observation = projectObservation(snapshot, dataMode);
  const next = { ...previous, seen: new Set(previous.seen), observationKey: observationKey(observation) };

  const phase = snapshot.sections.phase?.status === 'ok' ? snapshot.sections.phase.data : undefined;
  const gameflow = snapshot.sections.gameflow?.status === 'ok' ? snapshot.sections.gameflow.data : undefined;
  const gameIdValue = gameflow?.gameData?.gameId ?? gameflow?.gameId;
  const gameId = gameIdValue && String(gameIdValue) !== '0' ? String(gameIdValue) : undefined;
  const live = snapshot.sections.live?.status === 'ok' ? snapshot.sections.live.data : undefined;
  const gameTime = Number(live?.gameData?.gameTime);
  const hasTime = live && Number.isFinite(gameTime);
  const events = live?.events?.Events ?? [];
  const inGame = new Set(['InProgress', 'Reconnect', 'GameStart']);
  const beforeGame = new Set(['None', 'Lobby', 'Matchmaking', 'ReadyCheck', 'ChampSelect']);
  if (phase && beforeGame.has(phase)) next.leftGame = true;

  let sessionChange;
  if (gameId && previous.gameId && gameId !== previous.gameId) sessionChange = 'client_game_id_changed';
  else if (hasTime && previous.lastGameTime !== undefined && gameTime < previous.lastGameTime - 3) sessionChange = 'game_clock_restarted';
  else if (live && next.leftGame && phase && inGame.has(phase)) sessionChange = 'client_entered_new_game';

  if (sessionChange) {
    next.epoch++;
    next.seen.clear();
    // A watcher already running across a verified game boundary should receive the
    // new game's first events, including GameStart, even if their IDs are reused.
    next.eventsInitialized = true;
    next.lastGameTime = undefined;
    next.leftGame = false;
  }
  if (gameId) next.gameId = gameId;
  if (phase) next.phase = phase;
  if (hasTime) next.lastGameTime = gameTime;

  const newEvents = [];
  if (live) {
    for (const event of events) {
      const key = eventKey(event);
      if (next.eventsInitialized && !next.seen.has(key)) newEvents.push(event);
      next.seen.add(key);
    }
    // Only the first successful live snapshot after starting a watch is a baseline.
    // Subsequent game boundaries are explicitly labelled and include new-game events.
    next.eventsInitialized = true;
  }
  next.initial = false;
  if (!previous.initial && previous.observationKey === next.observationKey && !newEvents.length && !sessionChange) return { next, update: null };
  return { next, update: {
    type: 'lol_update', observedAt: snapshot.capturedAt, initial: previous.initial,
    observation, events: newEvents,
    ...(sessionChange ? { sessionChange, eventsFromNewSession: true } : {}),
  } };
}

const systemClock = { now: () => Date.now(), setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: timer => clearTimeout(timer) };

function samplingInterval(value) {
  if (!Number.isFinite(value) || value <= 0) throw new TypeError('intervalMs must be positive');
  return Math.max(250, value);
}

async function cancellable(promise, signal) {
  checkAbort(signal);
  let cancel;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      cancel = () => reject(aborted()); signal.addEventListener('abort', cancel, { once: true });
    })]);
  } finally { signal.removeEventListener('abort', cancel); }
}

export function createGameWatcher({ connector, onUpdate, intervalMs = 1000, dataMode = 'basic', clock = systemClock } = {}) {
  if (!connector?.read || typeof onUpdate !== 'function') throw new Error('connector and onUpdate are required');
  let interval = samplingInterval(intervalMs);
  let selectedMode = validateDataMode(dataMode);
  let controller, task, state = initialState(), pending, sequence = 0, latestSnapshot = null;
  let rescheduleSleep;
  let diagnostic = { running: false, pendingDelivery: false, lastError: null, lastDeliveredAt: null };
  const runId = randomUUID();

  function waitForNextPoll(started, signal) {
    if (signal.aborted) return Promise.reject(aborted());
    return new Promise((resolve, reject) => {
      let timer;
      const clean = () => {
        clock.clearTimeout(timer);
        signal.removeEventListener('abort', cancel);
        if (rescheduleSleep === schedule) rescheduleSleep = undefined;
      };
      const cancel = () => { clean(); reject(aborted()); };
      const schedule = () => {
        clock.clearTimeout(timer);
        timer = clock.setTimeout(() => { clean(); resolve(); }, Math.max(0, started + interval - clock.now()));
      };
      signal.addEventListener('abort', cancel, { once: true });
      rescheduleSleep = schedule;
      schedule();
    });
  }

  async function run(signal) {
    while (!signal.aborted) {
      const started = clock.now();
      try {
        if (!pending) {
          latestSnapshot = await connector.read({ signal });
          checkAbort(signal);
          const detected = detectUpdate(latestSnapshot, state, { dataMode: selectedMode });
          if (detected.update) {
            pending = { update: { id: `${runId}:${++sequence}`, ...detected.update }, next: detected.next };
            diagnostic.pendingDelivery = true;
          } else state = detected.next;
        }
        if (pending) {
          const acknowledgement = await cancellable(Promise.resolve().then(() => {
            checkAbort(signal); return onUpdate(pending.update, { signal });
          }), signal);
          if (acknowledgement === false) throw new Error('Delivery was not acknowledged');
          checkAbort(signal);
          state = pending.next;
          pending = undefined;
          diagnostic.pendingDelivery = false;
          diagnostic.lastDeliveredAt = new Date(clock.now()).toISOString();
        }
        diagnostic.lastError = null;
      } catch (error) {
        if (signal.aborted) break;
        diagnostic.lastError = safeReason(error);
      }
      try { await waitForNextPoll(started, signal); } catch { break; }
    }
    diagnostic.running = false;
  }

  return {
    start() {
      if (diagnostic.running) return;
      controller = new AbortController();
      state = initialState(); pending = undefined;
      diagnostic = { running: true, pendingDelivery: false, lastError: null, lastDeliveredAt: null };
      task = run(controller.signal);
    },
    async stop() {
      controller?.abort();
      await task;
      pending = undefined;
      diagnostic.pendingDelivery = false;
    },
    setIntervalMs(value) {
      interval = samplingInterval(value);
      rescheduleSleep?.();
      return interval;
    },
    setDataMode(value) {
      selectedMode = validateDataMode(value);
      return selectedMode;
    },
    status() { return { ...diagnostic, intervalMs: interval, dataMode: selectedMode }; },
    get latestSnapshot() { return latestSnapshot; },
  };
}
