#!/usr/bin/env node
/** Run a Python script with the sidecar virtual environment (Windows paths safe).
 *
 *   node vision/run.mjs probe.py --image shot.png
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const python = join(here, '.venv', 'Scripts', 'python.exe');
const script = process.argv[2];
if (!script) {
  console.error('usage: node vision/run.mjs <script.py> [args...]');
  process.exit(2);
}
if (!existsSync(python)) {
  console.error('缺少 Python 环境。先运行：powershell -ExecutionPolicy Bypass -File vision\\setup.ps1');
  process.exit(2);
}
const child = spawn(python, [join(here, script), ...process.argv.slice(3)], {
  cwd: join(here, '..'), stdio: 'inherit', windowsHide: false,
});
child.on('exit', code => process.exit(code ?? 0));
