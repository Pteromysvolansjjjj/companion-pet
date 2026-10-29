#!/usr/bin/env node
/**
 * Show exactly what the screen-OCR path would send to the model.
 *
 *   node scripts/screen-demo.mjs                       # one live frame
 *   node scripts/screen-demo.mjs shot.png              # a screenshot
 *   node scripts/screen-demo.mjs vision/samples/synthetic-hud.png --json
 *
 * It runs the real sidecar, maps the frame with the real connector, then prints
 * the batched user message the plugin would deliver to the bound conversation.
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createScreenConnector } from '../src/vision/connector.mjs';
import { detectUpdate } from '../src/game/watcher.mjs';
import { projectObservation } from '../src/game/projection.mjs';
import { createSpeechContract } from '../src/replies.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const python = join(root, 'vision', '.venv', 'Scripts', 'python.exe');
const script = join(root, 'vision', 'main.py');

const args = process.argv.slice(2);
const json = args.includes('--json');
const image = args.find(value => !value.startsWith('--'));

if (!existsSync(python)) {
  console.error('缺少 Python 环境：先运行 vision/setup.ps1 安装依赖。');
  process.exit(2);
}

const command = [script, '--root', root, '--frames', '1', '--no-voice'];
if (image) command.push('--image', resolve(image));

const child = spawn(python, command, { cwd: root, windowsHide: true });
let buffer = '';
let frame = null;
child.stdout.on('data', chunk => {
  buffer += chunk.toString('utf8');
  const lines = buffer.split('\n');
  buffer = lines.pop() ?? '';
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const message = JSON.parse(line);
      if (message.type === 'frame') frame = message;
      if (message.type === 'error') console.error('[sidecar]', message.code, message.message);
    } catch { /* ignore non-protocol output */ }
  }
});
child.stderr.on('data', () => {});
child.on('exit', () => {
  if (!frame) {
    console.error('没有取到画面。确认游戏在窗口/无边框模式下处于前台，或传入一张截图。');
    process.exit(1);
  }
  const connector = createScreenConnector({ bridge: { latestFrame: () => frame, status: () => ({ frames: 1 }) } });
  const snapshot = connector.read();
  const projected = projectObservation(snapshot, 'basic');
  const { update } = detectUpdate(snapshot, undefined, { dataMode: 'basic' });
  const message = '[LOL 实时数据]\n' + createSpeechContract({ replyLength: 20, dataMode: 'basic', dataSource: 'screen' }) + '\n'
    + JSON.stringify({ ...update, observation: projected });

  const screen = projected.sections.screen.data;
  console.log('— 画面 —');
  console.log('窗口:', frame.window.title, `${frame.window.width}x${frame.window.height}`, `耗时 ${frame.durationMs}ms`);
  console.log('识别文字:');
  for (const line of screen.lines) console.log(`  [${line.region}] ${line.text}`);
  console.log('结构化字段:', JSON.stringify(screen.fields));
  if (screen.board) console.log('记分板行数:', screen.board.rows.length);
  if (update?.events?.length) console.log('本批新增事件:', JSON.stringify(update.events));

  console.log('\n— 投递给模型的文本（' + message.length + ' 字符）—');
  console.log(json ? message : message.slice(0, 1800) + (message.length > 1800 ? '\n…（截断，加 --json 看全文）' : ''));
});
