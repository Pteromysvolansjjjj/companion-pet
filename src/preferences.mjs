import { existsSync, readFileSync, mkdirSync, writeFileSync, renameSync } from 'node:fs';
import { dirname, join } from 'node:path';

export const preferenceDefaults = Object.freeze({
  pollIntervalMs: 1000, decisionIntervalMs: 15000, bubbleHoldMs: 5000, replyLength: 20,
  dataMode: 'basic',
});
const ranges = {
  pollIntervalMs: [250, 2147483647], decisionIntervalMs: [500, 2147483647],
  bubbleHoldMs: [500, 30000], replyLength: [0, Number.MAX_SAFE_INTEGER],
};
export function validatePreferences(input) {
  const result = {};
  for (const [key, value] of Object.entries(input)) {
    if (key === 'dataMode') {
      if (!['basic', 'detailed'].includes(value)) throw new Error('dataMode must be basic or detailed');
      result[key] = value; continue;
    }
    if (!Object.hasOwn(ranges, key)) throw new Error('Unknown LOL setting: ' + key);
    const [min, max] = ranges[key];
    if (!Number.isSafeInteger(value) || value < min || value > max) throw new Error(`${key} must be an integer between ${min} and ${max}`);
    result[key] = value;
  }
  return result;
}

/** Store only explicit UI/transport preferences, never role memory or game records. */
export function createPreferenceStore({ root, config = {}, persist = true }) {
  const defaults = { ...preferenceDefaults };
  for (const key of Object.keys(defaults)) if (config[key] !== undefined) defaults[key] = config[key];
  validatePreferences(defaults);
  const path = join(root, 'data', 'conversation-settings.json');
  let sessions = Object.create(null);
  if (persist && existsSync(path)) {
    const saved = JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''));
    if (saved.version !== 1 || !saved.sessions || typeof saved.sessions !== 'object' || Array.isArray(saved.sessions)) throw new Error('Invalid LOL conversation settings file');
    for (const [id, value] of Object.entries(saved.sessions)) sessions[id] = validatePreferences(value);
  }
  return {
    get(id) { return { ...defaults, ...(Object.hasOwn(sessions, id) ? sessions[id] : {}) }; },
    set(id, changes) {
      const patch = validatePreferences(changes);
      if (!Object.keys(patch).length) return this.get(id);
      const next = Object.assign(Object.create(null), sessions);
      next[id] = { ...(Object.hasOwn(next, id) ? next[id] : {}), ...patch };
      if (persist) {
        mkdirSync(dirname(path), { recursive: true });
        writeFileSync(path + '.tmp', JSON.stringify({ version: 1, sessions: next }, null, 2));
        renameSync(path + '.tmp', path);
      }
      sessions = next;
      return this.get(id);
    },
  };
}
