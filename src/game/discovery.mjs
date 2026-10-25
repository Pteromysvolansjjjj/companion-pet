import { readFile, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { checkAbort } from './sanitize.mjs';

const execFileAsync = promisify(execFile);
const validPort = value => Number.isInteger(Number(value)) && Number(value) > 0 && Number(value) <= 65535;
export const DEFAULT_INSTALL_ROOTS = Object.freeze(['C:', 'D:', 'E:'].flatMap(drive => [
  `${drive}\\Riot Games\\League of Legends`, `${drive}\\WeGameApps\\英雄联盟`,
  `${drive}\\Program Files\\WeGameApps\\英雄联盟`, `${drive}\\Program Files (x86)\\WeGameApps\\英雄联盟`,
  `${drive}\\腾讯游戏\\英雄联盟`, `${drive}\\英雄联盟`,
]));

// This result is an internal credential object. Never put it in snapshots or diagnostics.
export function parseLockfile(text) {
  const parts = String(text).trim().split(':');
  if (parts.length !== 5 || !/^LeagueClient(?:Ux)?$/i.test(parts[0]) || !validPort(parts[2]) || !parts[3] || parts[4] !== 'https') return null;
  return { port: Number(parts[2]), password: parts[3], pid: Number(parts[1]) || undefined, source: 'lockfile' };
}

function argument(line, name) {
  // Windows command lines sometimes quote the entire --name=value argument.
  const expression = new RegExp(`(?:^|[\\s"'])--${name}=(?:"([^"\\r\\n]*)"|'([^'\\r\\n]*)'|([^\\s"']+))`, 'i');
  const match = String(line).match(expression);
  return match && (match[1] ?? match[2] ?? match[3]);
}

export function parseClientCommandLine(line) {
  const port = argument(line, 'app-port');
  const password = argument(line, 'remoting-auth-token');
  if (!validPort(port) || !password) return null;
  return { port: Number(port), password, source: 'process' };
}

/** Some regional clients log their local bootstrap URL instead of command-line flags. */
export function parseBootstrapUrl(line) {
  const urls = String(line).match(/https?:\/\/[^\s"'<>]+/gi) ?? [];
  for (const candidate of urls) {
    // Check the literal authority too: URL normalization must not expand the allowed
    // hosts to integer/hex IP aliases, remote hosts, extra userinfo or implicit ports.
    const literal = candidate.match(/^https:\/\/riot:([^@\s]+)@(127\.0\.0\.1|localhost):([0-9]+)\/bootstrap\.html(?:[?#].*)?$/i);
    if (!literal || !validPort(literal[3])) continue;
    try {
      const url = new URL(candidate);
      if (url.protocol !== 'https:' || url.username !== 'riot' || !['127.0.0.1', 'localhost'].includes(url.hostname) || url.pathname !== '/bootstrap.html') continue;
      const password = decodeURIComponent(url.password);
      if (!password || /[\r\n\0]/.test(password)) continue;
      return { port: Number(literal[3]), password, source: 'recent_log' };
    } catch { /* A malformed URL is not a credential source. */ }
  }
  return null;
}

async function queryClientProcesses(signal) {
  if (process.platform !== 'win32') return [];
  const command = 'Get-CimInstance Win32_Process -Filter "Name = \'LeagueClientUx.exe\'" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress';
  const { stdout } = await execFileAsync('powershell.exe', ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command], {
    windowsHide: true, timeout: 5000, maxBuffer: 256 * 1024, signal,
  });
  if (!stdout.trim()) return [];
  const parsed = JSON.parse(stdout.replace(/^\uFEFF/, ''));
  return Array.isArray(parsed) ? parsed : [parsed];
}

export function createCredentialDiscovery(options = {}) {
  const io = options.fs ?? { readFile, readdir, stat };
  const processQuery = options.processQuery ?? queryClientProcesses;
  let cached;
  let lastMissAt = 0;

  async function discover({ signal, refresh = false } = {}) {
    checkAbort(signal);
    if (cached && !refresh) return cached;
    if (!refresh && Date.now() - lastMissAt < (options.discoveryRetryMs ?? 5000)) return null;
    const roots = [...(options.installRoots ?? DEFAULT_INSTALL_ROOTS)];
    let processes = [];
    if (options.discoverProcess !== false) {
      try { processes = await processQuery(signal); } catch { checkAbort(signal); }
      for (const proc of processes) if (proc.ExecutablePath) roots.push(path.dirname(proc.ExecutablePath));
    }
    const candidates = new Set(options.lockfilePaths ?? []);
    for (const root of roots) {
      candidates.add(path.join(root, 'lockfile'));
      candidates.add(path.join(root, 'LeagueClient', 'lockfile'));
    }
    for (const filename of candidates) {
      checkAbort(signal);
      try {
        const info = await io.stat(filename);
        if (!info.isFile() || info.size > 16 * 1024) continue;
        const parsed = parseLockfile(await io.readFile(filename, 'utf8'));
        if (parsed) return cached = parsed;
      } catch { checkAbort(signal); }
    }
    for (const proc of processes) {
      const parsed = parseClientCommandLine(proc.CommandLine);
      if (parsed) return cached = parsed;
    }
    // Known installation roots plus explicit log directories only; no recursive disk scan.
    const logDirectories = new Set(options.logDirectories ?? []);
    for (const root of roots) {
      logDirectories.add(root);
      logDirectories.add(path.join(root, 'LeagueClient'));
      logDirectories.add(path.join(root, 'Logs', 'LeagueClient Logs'));
      logDirectories.add(path.join(root, 'LeagueClient', 'Logs', 'LeagueClient Logs'));
    }
    const activePids = new Set(processes.map(proc => Number(proc.ProcessId)).filter(pid => Number.isSafeInteger(pid) && pid > 0));
    const newest = [];
    for (const directory of logDirectories) {
      checkAbort(signal);
      try {
        const entries = await io.readdir(directory, { withFileTypes: true });
        for (const entry of entries) {
          // Exclude Helper/renderer variants: only the UX process's own log is a credential source.
          if (!entry.isFile() || !/LeagueClientUx\.log$/i.test(entry.name)) continue;
          const filename = path.join(directory, entry.name);
          const info = await io.stat(filename);
          const age = Date.now() - info.mtimeMs;
          const activeProcessLog = entry.name.split('_').some(part => /^\d+$/.test(part) && activePids.has(Number(part)));
          const recent = age < (options.logMaxAgeMs ?? 2 * 60 * 60 * 1000);
          if (age >= -60_000 && (recent || activeProcessLog) && info.size <= 8 * 1024 * 1024) newest.push({ filename, time: info.mtimeMs });
        }
      } catch { checkAbort(signal); }
    }
    newest.sort((a, b) => b.time - a.time);
    for (const item of newest.slice(0, 3)) {
      checkAbort(signal);
      try {
        const content = await io.readFile(item.filename, 'utf8');
        for (const line of content.split(/\r?\n/).reverse()) {
          const parsed = parseClientCommandLine(line) ?? parseBootstrapUrl(line);
          if (parsed) return cached = { ...parsed, source: 'recent_log' };
        }
      } catch { checkAbort(signal); }
    }
    lastMissAt = Date.now();
    return null;
  }

  return { discover, invalidate() { cached = undefined; lastMissAt = 0; } };
}
