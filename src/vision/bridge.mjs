import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { createInterface } from 'node:readline';

/**
 * Host <-> Python sidecar transport.
 *
 * The sidecar does the two things Node cannot do cheaply on Windows: desktop
 * capture with OCR, and microphone capture with local speech recognition.
 * It holds no model access and no game credentials; it emits plain text.
 */
export function createVisionBridge({
  root,
  pythonPath = join(root, 'vision', '.venv', 'Scripts', 'python.exe'),
  scriptPath = join(root, 'vision', 'main.py'),
  intervalMs = 1000,
  enabled = true,
  spawnProcess = spawn,
  onError = () => {},
} = {}) {
  let child = null;
  let ready = false;
  let closing = false;
  let restartTimer = null;
  let restarts = 0;
  let lastError = null;
  let latestFrame = null;
  let frameCount = 0;
  let stderrTail = '';
  const listeners = new Map();
  const pending = [];

  function on(event, listener) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(listener);
    return () => listeners.get(event)?.delete(listener);
  }
  function notify(event, payload) {
    for (const listener of listeners.get(event) ?? []) {
      try { listener(payload); } catch { /* a listener must not break the pipe */ }
    }
  }
  function report(code, detail) {
    lastError = code;
    onError(code, detail);
    notify('error', { code, detail });
  }
  function write(message) {
    if (!child?.stdin?.writable) return false;
    try { child.stdin.write(JSON.stringify(message) + '\n'); return true; }
    catch { report('VISION_PIPE_CLOSED'); return false; }
  }
  function flush() {
    while (pending.length) write(pending.shift());
  }

  function start() {
    if (!enabled) return false;
    if (closing) return false;
    if (child && !child.killed) return true;
    if (!existsSync(pythonPath) || !existsSync(scriptPath)) {
      report('VISION_RUNTIME_MISSING');
      return false;
    }
    try {
      child = spawnProcess(pythonPath, [scriptPath, '--root', root, '--interval', String(intervalMs)],
        { cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch {
      report('VISION_START_FAILED');
      child = null;
      return false;
    }
    ready = false;
    stderrTail = '';
    const instance = child;
    instance.on('error', () => {
      if (child !== instance) return;
      report('VISION_START_FAILED');
      child = null;
      ready = false;
    });
    instance.stdin.on('error', () => { if (child === instance) report('VISION_PIPE_CLOSED'); });
    const lines = createInterface({ input: instance.stdout });
    lines.on('line', line => {
      if (child !== instance) return;
      let message;
      try { message = JSON.parse(line); } catch { return; }
      if (!message || typeof message.type !== 'string') return;
      if (message.type === 'ready') {
        ready = true;
        lastError = null;
        restarts = 0;
        notify('ready', message);
        flush();
        return;
      }
      if (message.type === 'frame') {
        // Only the newest frame is retained: the host samples, it does not queue.
        latestFrame = message;
        frameCount++;
        notify('frame', message);
        return;
      }
      if (message.type === 'voice') { notify('voice', message); return; }
      if (message.type === 'voice:state') { notify('voice:state', message); return; }
      if (message.type === 'status') { notify('status', message); return; }
      if (message.type === 'error') { report(String(message.code || 'VISION_ERROR').toUpperCase(), message.message); return; }
      if (message.type === 'exit') { notify('exit', message); }
    });
    instance.stderr.on('data', chunk => {
      // Keep a short tail for diagnostics; never forward raw output to the model.
      stderrTail = (stderrTail + chunk.toString('utf8')).slice(-2000);
    });
    instance.on('exit', () => {
      lines.close();
      if (child !== instance) return;
      child = null;
      ready = false;
      notify('exit', null);
      if (!closing && enabled && restarts < 3) {
        restarts++;
        restartTimer = setTimeout(() => { restartTimer = null; if (!closing) start(); }, 2000 * restarts);
        restartTimer.unref?.();
      }
    });
    return true;
  }

  function send(message) {
    if (!enabled || closing) return false;
    if (!child) { pending.push(message); return start(); }
    if (!ready) { pending.push(message); return true; }
    return write(message);
  }

  return {
    start,
    send,
    on,
    onError(listener) { return on('error', listener); },
    latestFrame: () => latestFrame,
    // Disabling must never start the sidecar just to tell it to stop.
    setOcr(enabledOcr) {
      if (!enabledOcr && !child) return true;
      return send({ type: 'ocr', enabled: Boolean(enabledOcr) });
    },
    setVoice(config) { return send({ type: 'voice', enabled: true, config }); },
    stopVoice() {
      if (!child) return true;
      return send({ type: 'voice', enabled: false });
    },
    requestSnapshot() { return send({ type: 'snapshot' }); },
    status() {
      return {
        enabled, running: Boolean(child) && !child.killed, ready,
        frames: frameCount, lastError,
        frame: latestFrame ? { id: latestFrame.id, capturedAt: latestFrame.capturedAt, window: latestFrame.window } : null,
        stderrTail: stderrTail ? stderrTail.slice(-400) : null,
      };
    },
    close() {
      closing = true;
      if (restartTimer) clearTimeout(restartTimer);
      const instance = child;
      child = null;
      ready = false;
      latestFrame = null;
      if (!instance) return;
      try { instance.stdin.write(JSON.stringify({ type: 'shutdown' }) + '\n'); } catch { /* best effort */ }
      try { instance.stdin.end(); } catch { /* best effort */ }
      const timer = setTimeout(() => { if (!instance.killed) instance.kill(); }, 1500);
      timer.unref?.();
    },
  };
}
