import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { createInterface } from 'node:readline';

/** Desktop display/preferences and explicit human text; no credentials, game state or model access. */
export function createDesktopBridge({ root, spawnProcess = spawn, onError = () => {}, enabled = true } = {}) {
  let process = null;
  let pending = [];
  let ready = false;
  let lastError = null;
  let closing = false;
  let exitAnnounced = false;
  let playback = false;
  let commandSequence = 0;
  let expectedCommandId = null;
  const playbackListeners = new Set();
  const preferenceListeners = new Set();
  const chatSubmitListeners = new Set();
  const readyListeners = new Set();
  const executable = join(root, 'dist', 'LolCompanion-0.7.1.exe');
  function notifyPlayback(active) {
    playback = active;
    for (const listener of playbackListeners) listener(active);
  }
  function report(code) { lastError = code; onError(code); }
  function write(message) {
    if (!process?.stdin?.writable) { notifyPlayback(false); return false; }
    try { process.stdin.write(JSON.stringify(message) + '\n'); return true; }
    catch { report('DESKTOP_PIPE_CLOSED'); notifyPlayback(false); return false; }
  }
  function start() {
    if (!enabled || closing) return false;
    if (process && !process.killed && !exitAnnounced) return true;
    // An explicit start may replace a renderer that announced exit but has not
    // completed OS teardown yet. Ordinary traffic never takes this path.
    const previous = process;
    process = null;
    ready = false;
    pending = [];
    exitAnnounced = false;
    expectedCommandId = null;
    notifyPlayback(false);
    if (previous && !previous.killed) previous.kill();
    if (!existsSync(executable)) { report('DESKTOP_NOT_BUILT'); return false; }
    try {
      const child = spawnProcess(executable, [], { cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
      process = child;
      ready = false;
      child.on('error', () => {
        if (process !== child) return;
        report('DESKTOP_START_FAILED');
        process = null; ready = false; pending = []; exitAnnounced = false;
        notifyPlayback(false);
      });
      child.stdin.on('error', () => { if (process === child && !exitAnnounced && !closing) { report('DESKTOP_PIPE_CLOSED'); notifyPlayback(false); } });
      const lines = createInterface({ input: child.stdout });
      lines.on('line', line => {
        if (process !== child) return;
        try {
          const message = JSON.parse(line);
          if (message.type === 'exit') {
            exitAnnounced = true;
            ready = false;
            pending = [];
            notifyPlayback(false);
          } else if (message.type === 'ready' && !exitAnnounced) {
            ready = true;
            lastError = null;
            for (const item of pending.splice(0)) write(item);
            for (const listener of readyListeners) listener();
          } else if (message.type === 'preferences:set' && !exitAnnounced) {
            for (const listener of preferenceListeners) listener(message);
          } else if (message.type === 'chat:submit' && ready && !exitAnnounced && !closing) {
            for (const listener of chatSubmitListeners) listener(message);
          } else if (message.type === 'playback' && (message.commandId ?? null) === expectedCommandId && typeof message.active === 'boolean' && !exitAnnounced) {
            notifyPlayback(message.active);
          } else if (message.type === 'invalid' && message.commandId === expectedCommandId && typeof message.active === 'boolean' && !exitAnnounced) {
            expectedCommandId = message.currentCommandId ?? null;
            report('DESKTOP_REJECTED_MESSAGE');
            notifyPlayback(message.active);
          }
        } catch { /* Only protocol messages are consumed. */ }
      });
      // Do not forward arbitrary process output into the conversation or diagnostics.
      child.stderr.on('data', () => { if (process === child) report('DESKTOP_REPORTED_ERROR'); });
      child.on('exit', () => { lines.close(); if (process === child) { process = null; ready = false; pending = []; exitAnnounced = false; notifyPlayback(false); } });
      return true;
    } catch { report('DESKTOP_START_FAILED'); return false; }
  }
  function send(message, { launch = message?.type === 'show' } = {}) {
    if (!enabled || closing) return false;
    // Replies/status/clear can update an existing renderer, but cannot resurrect
    // one the user exited. Only start() or an explicit show opens a process.
    if ((!process || process.killed || exitAnnounced) && (message?.type !== 'show' || !launch || !start())) return false;
    if (message?.type === 'reply') {
      // Match the renderer's transport envelope; it must never leave the host busy after rejection.
      if (JSON.stringify(message).length > 16 * 1024 * 1024 - 128 || message.lines?.some(line => line.length > 4096)) { report('DESKTOP_REPLY_TRANSPORT_LIMIT'); return false; }
    }
    if (['reply', 'clear', 'hide'].includes(message?.type)) {
      expectedCommandId = String(++commandSequence);
      message = { ...message, commandId: expectedCommandId };
      notifyPlayback(message.type === 'reply');
    }
    if (ready) return write(message);
    // Initial display state needs only the latest reply/status, never an expanding playback queue.
    pending = pending.filter(item => item.type !== message.type && !(['clear', 'hide'].includes(message.type) && item.type === 'reply'));
    pending.push(message);
    return true;
  }
  return {
    start,
    send,
    onPlayback(listener) { playbackListeners.add(listener); return () => playbackListeners.delete(listener); },
    onPreferences(listener) { preferenceListeners.add(listener); return () => preferenceListeners.delete(listener); },
    onChatSubmit(listener) { chatSubmitListeners.add(listener); return () => chatSubmitListeners.delete(listener); },
    onReady(listener) { readyListeners.add(listener); return () => readyListeners.delete(listener); },
    status: () => ({ enabled, running: !!process && !process.killed && !exitAnnounced, ready, playback, error: lastError }),
    close() {
      closing = true;
      pending = [];
      notifyPlayback(false);
      const child = process;
      if (!child) return;
      write({ type: 'shutdown' });
      child.stdin.end();
      const timeout = setTimeout(() => { if (process === child) child.kill(); }, 2000);
      timeout.unref?.();
    },
  };
}
