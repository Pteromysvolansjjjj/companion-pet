import { randomUUID } from 'node:crypto';
import { canonical, reverseDelta } from './delta.mjs';

// Bound sampled process history, not the latest observation or new game events.
// Count the JSON array delimiters and commas as well as each step's UTF-8 bytes.
const MAX_HISTORY_STEPS = 20;
const MAX_HISTORY_BYTES = 64 * 1024;

const systemClock = {
  now: () => Date.now(),
  setTimeout: (fn, milliseconds) => setTimeout(fn, milliseconds),
  clearTimeout: timer => clearTimeout(timer),
};

function emptyBuffer() {
  return {
    sourceUpdateIds: [], sections: new Map(), events: [], sessionChanges: [],
    historySteps: [], historyStepBytes: [], historyBytes: 2, omittedStepCount: 0,
    initial: false,
  };
}

function appendHistoryStep(buffer, step) {
  const bytes = Buffer.byteLength(JSON.stringify(step), 'utf8');
  buffer.historyBytes += bytes + (buffer.historySteps.length ? 1 : 0);
  buffer.historySteps.push(step);
  buffer.historyStepBytes.push(bytes);
  // Drop oldest steps first so replay always follows a contiguous reverse chain.
  // An oversized newest step therefore also drops all preceding steps: retaining
  // an older patch without that link would replay it against the wrong snapshot.
  while (buffer.historySteps.length > MAX_HISTORY_STEPS || buffer.historyBytes > MAX_HISTORY_BYTES) {
    buffer.historyBytes -= buffer.historyStepBytes.shift() + (buffer.historySteps.length > 1 ? 1 : 0);
    buffer.historySteps.shift();
    buffer.omittedStepCount++;
  }
}

function appendUpdate(buffer, update, previousObservation) {
  if (!buffer.sourceUpdateIds.length) buffer.observedFrom = update.observedAt;
  buffer.sourceUpdateIds.push(update.id);
  buffer.observedAt = update.observedAt;
  buffer.initial ||= update.initial === true;
  for (const change of update.changes ?? []) buffer.sections.set(change.section, change);
  if (update.sessionChange) {
    buffer.sessionChanges.push({
      updateId: update.id, observedAt: update.observedAt, reason: update.sessionChange, beforeEventIndex: buffer.events.length,
    });
    // Keep undelivered events, but never attach an old game's context to a new one.
    delete buffer.context;
    delete buffer.self;
  }
  buffer.events.push(...(update.events ?? []));
  // Watcher contexts are complete observations. Missing live fields during a
  // disconnect must not be filled with stale health/roster data from an older one.
  if (update.context !== undefined) buffer.context = update.context;
  if (Object.hasOwn(update, 'self')) buffer.self = update.self;
  if (update.observation !== undefined) {
    if (previousObservation !== undefined) {
      appendHistoryStep(buffer, {
        fromObservedAt: update.observation.capturedAt,
        toObservedAt: previousObservation.capturedAt,
        patch: reverseDelta(update.observation, previousObservation),
      });
    }
    buffer.observation = update.observation;
  }
}

function makeBatch(buffer, id) {
  const { context, self, sessionChanges } = buffer;
  return {
    id, type: 'lol_update', batch: true,
    observedFrom: buffer.observedFrom,
    observedAt: buffer.observedAt,
    sourceUpdateIds: buffer.sourceUpdateIds,
    initial: buffer.initial,
    changes: [...buffer.sections.values()], events: buffer.events,
    ...(context !== undefined ? { context } : {}),
    ...(self !== undefined ? { self } : {}),
    ...(buffer.observation !== undefined ? {
      observation: buffer.observation,
      // Start at observation and apply these steps in array order. Each step
      // restores the previous sampled state, including its capturedAt value.
      // Truncation is explicit; retainedFrom is the oldest still reachable state.
      history: {
        encoding: 'reverse-set-delete-v1', base: 'observation',
        steps: [...buffer.historySteps].reverse(),
        truncated: buffer.omittedStepCount > 0,
        omittedStepCount: buffer.omittedStepCount,
        retainedFrom: buffer.historySteps[0]?.toObservedAt ?? buffer.observation.capturedAt,
        retainedSteps: buffer.historySteps.length,
        retainedBytes: buffer.historyBytes,
      },
    } : {}),
    ...(sessionChanges.length ? {
      sessionChange: sessionChanges.at(-1).reason,
      sessionChanges,
      eventsFromNewSession: true,
    } : {}),
  };
}

function cancelError() {
  const error = new Error('Batch delivery stopped');
  error.name = 'AbortError';
  return error;
}

async function abortable(promise, signal) {
  if (signal.aborted) throw cancelError();
  let onAbort;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      onAbort = () => reject(cancelError());
      signal.addEventListener('abort', onAbort, { once: true });
    })]);
  } finally {
    signal.removeEventListener('abort', onAbort);
  }
}

/**
 * Data collection can continue every second; automatic conversation delivery is batched.
 * push() acknowledges local buffering synchronously, not model consumption.
 * deliver(batch, {signal}) must resolve only after host acceptance; false/throw retries
 * the exact same batch ID. Consumers should also use that ID for idempotence.
 * Busy state is supplied by the host turn lifecycle; this module never judges content.
 */
export function createUpdateBatcher({ intervalMs = 15_000, deliver, clock = systemClock } = {}) {
  if (typeof deliver !== 'function') throw new TypeError('deliver is required');
  if (!Number.isFinite(intervalMs) || intervalMs <= 0) throw new TypeError('intervalMs must be positive');
  if (typeof clock.now !== 'function' || typeof clock.setTimeout !== 'function' || typeof clock.clearTimeout !== 'function') throw new TypeError('clock must provide now, setTimeout and clearTimeout');
  const runId = randomUUID();
  const controller = new AbortController();
  const acceptedUpdates = new Set();
  const acceptedEvents = new Set();
  let previousObservation;
  let queued = emptyBuffer();
  let pending;
  let timer;
  let busy = false;
  let stopped = false;
  let inFlight = false;
  let epoch = 0;
  let sequence = 0;
  let lastAttemptAt = null;
  let lastDeliveredAt = null;
  let lastError = null;
  let deliveredBatches = 0;

  const hasWork = () => queued.sourceUpdateIds.length > 0 || !!pending;
  const dueAt = () => (lastAttemptAt ?? queued.queuedAt ?? clock.now()) + intervalMs;
  function schedule() {
    if (stopped || busy || inFlight || timer !== undefined || !hasWork()) return;
    // Busy time does not reset the delivery deadline. Once the model/bubble is
    // free, overdue unattempted data is merged into one fresh batch immediately.
    timer = clock.setTimeout(() => { timer = undefined; void tick(); }, Math.max(0, dueAt() - clock.now()));
    timer?.unref?.();
  }

  async function tick() {
    if (stopped || !hasWork()) return;
    if (busy || inFlight) return;
    if (!pending) {
      pending = { batch: makeBatch(queued, `${runId}:${++sequence}`) };
      queued = emptyBuffer();
    }
    const attempt = pending;
    inFlight = true;
    lastAttemptAt = clock.now();
    // Retry deadlines and later deliveries are anchored to this attempt. A slow
    // acknowledgement never creates a concurrent delivery or resets that anchor.
    try {
      const accepted = await abortable(Promise.resolve().then(() => {
        if (controller.signal.aborted) throw cancelError();
        // Give consumers their own copy, protecting the retained retry payload.
        return deliver(structuredClone(attempt.batch), { signal: controller.signal });
      }), controller.signal);
      if (stopped) return;
      if (accepted === false) throw new Error('Host did not acknowledge the batch');
      if (pending === attempt) pending = undefined;
      lastDeliveredAt = clock.now();
      lastError = null;
      deliveredBatches++;
    } catch {
      if (!stopped) lastError = 'delivery_failed';
    } finally {
      inFlight = false;
      if (!stopped) schedule();
    }
  }

  return {
    push(value) {
      if (stopped) return false;
      if (!value || typeof value.id !== 'string' || !value.id) throw new TypeError('Updates need a non-empty string id');
      if (acceptedUpdates.has(value.id)) return true;
      const update = structuredClone(value);
      if (update.sessionChange) epoch++;
      update.events = (update.events ?? []).filter(event => {
        const key = `${epoch}:${JSON.stringify(canonical(event))}`;
        if (acceptedEvents.has(key)) return false;
        acceptedEvents.add(key);
        return true;
      });
      acceptedUpdates.add(update.id);
      // Keep the latest complete observation and a bounded contiguous reverse
      // history while busy. New game events are never pruned with sample history.
      if (!queued.sourceUpdateIds.length) queued.queuedAt = clock.now();
      appendUpdate(queued, update, previousObservation);
      if (update.observation !== undefined) previousObservation = update.observation;
      schedule();
      return true;
    },
    setIntervalMs(value) {
      if (!Number.isFinite(value) || value < 500) throw new TypeError('intervalMs must be at least 500');
      if (stopped) return intervalMs;
      intervalMs = value;
      if (timer !== undefined) clock.clearTimeout(timer);
      timer = undefined;
      schedule();
      return intervalMs;
    },
    setBusy(value) {
      if (stopped) return;
      busy = Boolean(value);
      schedule();
    },
    stop() {
      if (stopped) return;
      stopped = true;
      controller.abort();
      if (timer !== undefined) clock.clearTimeout(timer);
      timer = undefined;
      queued = emptyBuffer();
      pending = undefined;
      previousObservation = undefined;
      acceptedUpdates.clear();
      acceptedEvents.clear();
      inFlight = false;
      busy = false;
    },
    status() {
      return {
        stopped, busy, inFlight, intervalMs,
        bufferedUpdates: (pending?.batch.sourceUpdateIds.length ?? 0) + queued.sourceUpdateIds.length,
        bufferedEvents: (pending?.batch.events.length ?? 0) + queued.events.length,
        pendingBatchId: pending?.batch.id ?? null,
        lastAttemptAt, lastDeliveredAt, lastError, deliveredBatches,
      };
    },
  };
}
