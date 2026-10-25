import { createLeagueConnector } from '../src/game/index.mjs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
const root = dirname(dirname(fileURLToPath(import.meta.url)));
let config = {};
try { config = JSON.parse((await readFile(join(root, 'config.json'), 'utf8')).replace(/^\uFEFF/, '')); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const connector = createLeagueConnector({ ...config, caPath: join(root, 'assets', 'riotgames.pem') });
const snapshot = await connector.read({ signal: AbortSignal.timeout(25000) });
// Deliberately report keys/availability, not account identities or match contents.
const report = {
  checkedAt: snapshot.capturedAt,
  sources: snapshot.sources,
  phase: snapshot.sections.phase.status === 'ok' ? snapshot.sections.phase.data : null,
  sections: Object.fromEntries(Object.entries(snapshot.sections).map(([name, value]) => [name, {
    status: value.status,
    ...(value.reason ? { reason: value.reason } : {}),
    ...(value.status === 'ok' ? { fields: value.data && typeof value.data === 'object' ? Object.keys(value.data) : ['value'] } : {}),
  }])),
};
await mkdir(join(root, 'data'), { recursive: true });
await writeFile(join(root, 'data', 'probe-report.json'), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
