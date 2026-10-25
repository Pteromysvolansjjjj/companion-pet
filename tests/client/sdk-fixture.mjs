import { readFileSync } from 'node:fs';
import vm from 'node:vm';

export function source(initial) {
  let value = initial;
  const listeners = new Set();
  return { getSnapshot: () => value, subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); },
    set(next) { value = next; for (const fn of [...listeners]) fn(); },
    assign(next) { value = next; }, emit() { for (const fn of [...listeners]) fn(); } };
}

export function context() {
  const disposers = [];
  return { effect(fn) { const result = fn(); let done = false;
    const dispose = () => { if (done) return; done = true; if (typeof result === 'function') result(); };
    disposers.push(dispose); return dispose; }, dispose() { for (const fn of disposers.reverse()) fn(); } };
}

export function asarRead(packageName, path = 'lib/client.js') {
  const bytes = readFileSync(process.env.DSH_ASAR || 'E:/deepseek/dsh/resources/app.asar');
  const header = JSON.parse(bytes.subarray(16, 16 + bytes.readUInt32LE(12)));
  let node = header;
  for (const part of `dsh/node_modules/@deepseek-ai/${packageName}/${path}`.split('/')) node = node.files[part];
  const start = 8 + bytes.readUInt32LE(4) + Number(node.offset);
  return bytes.subarray(start, start + node.size).toString('utf8');
}

/** Loads unchanged SDK logic. Only test exports are appended; no installed file is written. */
export function sdkBrowser(packageName, expose = {}) {
  let factory;
  const schema = new Proxy(() => schema, { get: () => schema, apply: () => schema });
  class Service { static tracker = Symbol('tracker'); constructor(ctx, name) { this.ctx = ctx; ctx[name] = this; } }
  class Component {}
  const react = { Component, PureComponent: Component, memo: value => value, forwardRef: value => value,
    createContext: () => ({ Provider: () => null }), createElement: (type, props) => ({ type, props }) };
  const shared = {
    '@deepseek-ai/cordis': { Service },
    '@deepseek-ai/dsh-client-store': { createSnapshotStore: source, notifySubscribers: listeners => { for (const fn of [...listeners]) fn(); } },
    '@deepseek-ai/dsh-client-ui-slots': {},
    '@deepseek-ai/dsh-client-ui-primitives': new Proxy({}, { get: () => () => null }),
    react, 'react-dom': {}, 'react/jsx-runtime': { jsx: react.createElement, jsxs: react.createElement },
    '@tanstack/react-virtual': {},
    zod: { z: schema },
  };
  let code = asarRead(packageName);
  const marker = '\n\t\treturn module.exports;';
  if (!code.includes(marker)) throw new Error('SDK test export seam changed');
  const declarations = Object.entries(expose).map(([key, expression]) => `exports.${key} = ${expression};`).join('\n');
  code = code.replace(marker, '\n' + declarations + marker);
  vm.runInNewContext(code, { window: { __ModuleLoader__: { load: value => { factory = value.factory; } } },
    console, queueMicrotask, setTimeout, clearTimeout, URL, TextDecoder, TextEncoder, performance }, { filename: packageName });
  return factory(name => {
    if (!(name in shared)) throw new Error('Unmocked SDK presentation dependency: ' + name);
    return shared[name];
  });
}

export function loadProjectionSdk() {
  const conversation = sdkBrowser('dsh-client-ui-conversation');
  const chat = sdkBrowser('dsh-client-ui-chat', { register: 'registerConversationNodes' });
  const trajectory = sdkBrowser('dsh-client-ui-trajectory', { register: '(ctx) => { registerTrajectoryAssistantDefinition(ctx); registerTrajectoryCompactionDefinitions(ctx); registerTrajectoryMessageDefinitions(ctx); registerTrajectoryRequestHeaderDefinition(ctx); registerTrajectoryToolDefinition(ctx); registerTrajectoryConversationView(ctx); }' });
  return { conversation, chat, trajectory };
}
