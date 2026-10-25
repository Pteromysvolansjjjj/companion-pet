import { readFileSync, mkdirSync, writeFileSync, existsSync, renameSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash, randomUUID } from 'node:crypto';
import { createLeagueConnector, createGameWatcher, createUpdateBatcher } from './game/index.mjs';
import { createDesktopBridge } from './desktop.mjs';
import { createReplyCollector, createSpeechContract } from './replies.mjs';
import { createPreferenceStore, preferenceDefaults, validatePreferences } from './preferences.mjs';

const defaultRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const pluginVersion = JSON.parse(readFileSync(join(defaultRoot, 'package.json'), 'utf8')).version;
function loadConfig(root) {
  const path = join(root, 'config.json');
  if (!existsSync(path)) return {};
  try { return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, '')); }
  catch { throw new Error('LOL companion config.json is not valid JSON'); }
}

/** Dependency seams are for SDK and transport integration tests, not another chat backend. */
export function mountCompanion(ctx, sdk, supplied = {}, deps = {}) {
  const root = deps.root || defaultRoot;
  const config = { ...loadConfig(root), ...supplied };
  const preferences = createPreferenceStore({ root, config, persist: deps.persistPreferences ?? deps.persistStatus !== false });
  const sessionId = agent => String(agent.session.id ?? agent.id);
  const connector = deps.connector || createLeagueConnector({
    caPath: join(root, 'assets', 'riotgames.pem'),
    installRoots: config.installRoots,
    lockfilePaths: config.lockfilePaths,
    logDirectories: config.logDirectories,
    discoverProcess: config.discoverProcess !== false,
    requestTimeoutMs: config.requestTimeoutMs || 2500,
  });
  const desktop = deps.desktop || createDesktopBridge({ root, enabled: config.desktop !== false });
  const makeWatcher = deps.createWatcher || createGameWatcher;
  const makeBatcher = deps.createBatcher || createUpdateBatcher;
  let binding = null;
  let disposed = false;
  let disposeTask = null;
  let lastDiagnostic = null;
  let operations = Promise.resolve();
  const suppressedTurns = new WeakMap();
  const sessionTurns = new WeakMap();
  const disposedSessions = new WeakSet();
  function serial(action) {
    const task = operations.then(action);
    operations = task.catch(() => {});
    return task;
  }
  const registered = [];
  const runtimeFile = join(root, 'data', 'runtime-status.json');
  function status() {
    return {
      version: pluginVersion,
      loaded: !disposed,
      watching: !!binding,
      boundSessionId: binding ? String(binding.agent.session.id ?? binding.agent.id) : null,
      watcher: binding?.watcher?.status() ?? null,
      batching: binding?.batcher?.status() ?? null,
      preferences: binding ? { ...binding.preferences } : null,
      desktop: desktop.status(),
      diagnostic: lastDiagnostic,
    };
  }
  function record() {
    if (deps.persistStatus === false) return;
    // Diagnostic file contains connection/status only, never game payloads or credentials.
    try {
      mkdirSync(dirname(runtimeFile), { recursive: true });
      const summary = status();
      writeFileSync(runtimeFile + '.tmp', JSON.stringify({ version: pluginVersion, loaded: summary.loaded, watching: summary.watching, desktop: summary.desktop, diagnostic: summary.diagnostic, updatedAt: new Date().toISOString() }, null, 2));
      renameSync(runtimeFile + '.tmp', runtimeFile);
    } catch { /* Status I/O cannot interrupt data delivery. */ }
  }
  async function stopNow() {
    const previous = binding;
    binding = null;
    if (previous) {
      const suppressed = suppressedTurns.get(previous.agent.session) ?? new Set();
      for (const [turn, state] of previous.turns) if (state.automatic && !state.manual) suppressed.add(turn);
      suppressedTurns.set(previous.agent.session, suppressed);
      previous.batcher.stop();
      previous.collector.clear();
      previous.disposePrompt?.();
      for (const dispose of previous.registrations) dispose?.();
      // Remove only this listener's unclaimed inputs, never user prompts or active work.
      if (previous.agent.inbox) for (const target of ['next-turn', 'next-step']) {
        const pending = target === 'next-turn' ? previous.agent.inbox.nextTurn : previous.agent.inbox.nextStep;
        for (let i = pending.length - 1; i >= 0; i--) {
          if (previous.messageIds.has(pending[i].id)) previous.agent.inbox.splice(target, i, 1, []);
        }
      }
      await previous.watcher.stop();
    }
    desktop.send({ type: 'clear' }, { launch: false });
    desktop.send({ type: 'status', text: '监听已停止' }, { launch: false });
    sendPreferences();
    record();
  }
  async function startNow(agent) {
    if (disposed) throw new Error('LOL plugin is unloaded');
    if (!agent?.session || typeof agent.followup !== 'function' || disposedSessions.has(agent.session)) throw new Error('This DSH session does not expose the supported agent API');
    if (binding?.agent === agent) {
      desktop.send({ type: 'show' });
      sendPreferences();
      return status();
    }
    await stopNow();
    if (disposed) throw new Error('LOL plugin is unloaded');
    const current = {
      id: randomUUID(), agent, watcher: null, registrations: [], messageIds: new Set(), turns: new Map(), currentTurn: null,
      manualMessageIds: new Set(), acceptedChatRequests: new Map(),
      preferences: preferences.get(sessionId(agent)), playbackBusy: false, modelBusy: agent.status === 'running',
      collector: createReplyCollector(message => {
        if (binding !== current) return;
        current.playbackBusy = message.type === 'reply' && typeof desktop.onPlayback === 'function';
        syncBusy(current);
        const sent = desktop.send(message.type === 'reply' ? { ...message, holdMs: current.preferences.bubbleHoldMs } : message);
        if (sent === false) { current.playbackBusy = desktop.status().playback === true; syncBusy(current); }
      }, { onInvalid() {
        lastDiagnostic = 'speech_format_invalid'; record();
      } }),
    };
    function ownMessage(text) {
      const message = sdk.createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'lol-companion' } });
      current.messageIds.add(message.id);
      return message;
    }
    current.batcher = makeBatcher({
      intervalMs: current.preferences.decisionIntervalMs,
      clock: deps.clock,
      deliver(update, { signal }) {
        if (disposed || binding !== current || signal.aborted) throw new Error('LISTENER_STOPPED');
        if ((agent.status && agent.status !== 'idle') || current.playbackBusy || current.manualMessageIds.size > 0) { syncBusy(current); return false; }
        current.modelBusy = true; syncBusy(current);
        try {
          const contract = agent.ctx?.systemPrompt ? '' : createSpeechContract(current.preferences) + '\n';
          agent.followup(ownMessage('[LOL 实时数据]\n' + contract + JSON.stringify(update)));
          lastDiagnostic = null;
          desktop.send({ type: 'status', text: '正在接收 LOL 数据' }, { launch: false });
          record();
          if (typeof agent.whenIdle === 'function') void agent.whenIdle().then(() => {
            if (binding === current && !disposed) { current.modelBusy = false; syncBusy(current); }
          }, () => { if (binding === current) { current.modelBusy = false; syncBusy(current); } });
          return true;
        } catch (error) { current.modelBusy = false; syncBusy(current); throw error; }
      },
    });
    syncBusy(current);
    updatePrompt(current);
    if (agent.ctx?.on) {
      current.registrations.push(agent.ctx.on('agent/status', ({ status: phase }) => {
        if (binding === current) { current.modelBusy = phase !== 'idle'; syncBusy(current); }
      }));
    }
    current.watcher = makeWatcher({
      connector,
      intervalMs: current.preferences.pollIntervalMs,
      dataMode: current.preferences.dataMode,
      async onUpdate(update, { signal } = {}) {
        if (disposed || binding !== current || signal?.aborted) throw new Error('LISTENER_STOPPED');
        return current.batcher.push(update);
      },
    });
    binding = current;
    desktop.start();
    desktop.send({ type: 'clear' });
    desktop.send({ type: 'status', text: '已绑定当前对话 · 等待客户端数据' });
    sendPreferences();
    current.watcher.start();
    record();
    return status();
  }
  function syncBusy(current) {
    current.batcher?.setBusy(current.modelBusy || current.playbackBusy || current.manualMessageIds.size > 0 || current.turns.size > 0 || (current.agent.status && current.agent.status !== 'idle'));
  }
  function updatePrompt(current) {
    current.disposePrompt?.();
    current.disposePrompt = current.agent.ctx?.systemPrompt?.section({
      name: 'lol-companion:speech', order: 10300, interpolate: false, text: createSpeechContract(current.preferences),
    });
  }
  function sendPreferences(requestId, error) {
    desktop.send({ type: 'preferences', bindingId: binding?.id ?? null,
      values: binding ? { ...binding.preferences } : { ...preferenceDefaults },
      ...(requestId ? { requestId } : {}), ...(error ? { error } : {}),
    }, { launch: false });
  }
  function applyPreferences(agent, changes) {
    if (disposed) throw new Error('LOL plugin is unloaded');
    if (!agent?.session) throw new Error('This tool needs a DSH conversation');
    if (!changes || typeof changes !== 'object' || Array.isArray(changes)) throw new Error('Settings must be an object');
    const patch = validatePreferences(changes);
    const before = preferences.get(sessionId(agent));
    // A sub-second decision interval needs equally fresh sampling unless explicitly overridden.
    if (patch.decisionIntervalMs !== undefined && patch.pollIntervalMs === undefined && patch.decisionIntervalMs < before.pollIntervalMs) patch.pollIntervalMs = patch.decisionIntervalMs;
    const value = preferences.set(sessionId(agent), patch);
    const active = binding?.agent === agent;
    if (active && Object.keys(patch).length) {
      binding.preferences = value;
      binding.watcher.setIntervalMs(value.pollIntervalMs);
      binding.watcher.setDataMode(value.dataMode);
      binding.batcher.setIntervalMs(value.decisionIntervalMs);
      updatePrompt(binding);
    }
    record();
    return { sessionId: sessionId(agent), active, preferences: value, bubblePageCharacters: 20, replyLengthIsHardLimit: false };
  }
  const unsubscribePreferences = desktop.onPreferences?.(message => serial(() => {
    if (disposed) return;
    if (!binding || message?.bindingId !== binding.id) {
      sendPreferences(undefined, '当前绑定已变化，请重新打开设置确认。');
      return;
    }
    const requestId = typeof message.requestId === 'string' && message.requestId.length <= 128 ? message.requestId : undefined;
    if (!requestId) { sendPreferences(undefined, '设置请求无效，请重试。'); return; }
    try {
      applyPreferences(binding.agent, message.changes);
      sendPreferences(requestId);
    } catch {
      // Never expose file-system paths or raw I/O errors in the desktop UI.
      sendPreferences(requestId, '请检查数值范围，确认项目目录可写后重试。');
    }
  }).catch(() => {}));
  function sendChatResult(result) {
    // A lost acknowledgement must not turn an accepted user input into a retry
    // that enqueues it again. Successful requests are cached before this send.
    try { return desktop.send(result, { launch: false }); }
    catch { return false; }
  }
  const unsubscribeChatSubmit = desktop.onChatSubmit?.(message => serial(() => {
    const validId = value => typeof value === 'string' && value.length > 0 && value.length <= 128 && value.isWellFormed();
    const bindingId = validId(message?.bindingId) ? message.bindingId : null;
    const requestId = validId(message?.requestId) ? message.requestId : null;
    const reply = { type: 'chat:result', bindingId, requestId };
    const reject = error => sendChatResult({ ...reply, accepted: false, error });
    if (disposed || !binding || disposedSessions.has(binding.agent.session)) {
      reject('当前没有可用的陪玩对话，请在原对话中重新开启陪玩。'); return;
    }
    if (!bindingId || bindingId !== binding.id) {
      reject('绑定的对话已变化，请重新打开输入框后发送。'); return;
    }
    if (!requestId || message?.type !== 'chat:submit') {
      reject('发送请求无效，请重新发送。'); return;
    }
    const text = message.text;
    if (typeof text !== 'string' || text.length > 65536 || !text.isWellFormed() || !text.trim()) {
      reject('请输入有效文字，单次内容不能超过 65536 个字符。'); return;
    }
    const current = binding;
    const fingerprint = createHash('sha256').update(text, 'utf8').digest('hex');
    const prior = current.acceptedChatRequests.get(requestId);
    if (prior) {
      if (prior.fingerprint !== fingerprint) reject('该发送编号已用于其他内容，请重新发送。');
      else sendChatResult(prior.result);
      return;
    }
    let input;
    try {
      input = sdk.createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } });
      current.manualMessageIds.add(input.id);
      syncBusy(current);
      // This is ordinary human input to the bound original Agent. followup()
      // synchronously enqueues and wakes it, or queues its next turn while busy.
      // It never waits for the automatic interval/bubble or interrupts the model.
      current.agent.followup(input);
    } catch {
      if (input) current.manualMessageIds.delete(input.id);
      syncBusy(current);
      reject('当前对话未接收这条消息，请稍后重试。'); return;
    }
    const result = { ...reply, accepted: true };
    // Keep only a digest and acknowledgement; the Session owns actual user text.
    current.acceptedChatRequests.set(requestId, { fingerprint, result });
    sendChatResult(result);
    if (typeof current.agent.whenIdle === 'function') {
      const release = () => {
        if (binding !== current || disposed) return;
        current.manualMessageIds.delete(input.id);
        if (current.turns.size === 0) current.modelBusy = false;
        syncBusy(current);
      };
      // A cancelled input may never emit user/message. Quiescence also releases
      // its pending guard, without deleting any genuine user input or history.
      void current.agent.whenIdle().then(release, release);
    }
  }).catch(() => {}));
  const unsubscribeReady = desktop.onReady?.(() => sendPreferences());
  const unsubscribePlayback = desktop.onPlayback?.(active => {
    if (!binding) return;
    binding.playbackBusy = active;
    syncBusy(binding);
  });
  function addTool(definition) {
    const result = ctx.tools.register(sdk.defineTool({
      ...definition,
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }] },
    }));
    if (typeof result === 'function') registered.push(result);
  }
  addTool({
    name: 'lol_read',
    description: '只读查询英雄联盟客户端及对局：账号资料、大厅房间和模式、匹配、选人、实时对局、赛后；返回实际数据与连接状态。不会开启监听，不修改游戏。',
    parameters: { section: { type: 'string', description: '可选：all、phase、summoner、lobby、matchmaking、readyCheck、champSelect、endgame、gameflow、live；默认 all。' } },
    async execute(args, exec) {
      const snapshot = await connector.read({ signal: exec.signal });
      record();
      if (!args.section || args.section === 'all') return { snapshot, plugin: status() };
      if (!Object.hasOwn(snapshot.sections, args.section)) throw new Error('Unknown LOL data section');
      return { capturedAt: snapshot.capturedAt, section: args.section, result: snapshot.sections[args.section], plugin: status() };
    },
  });
  addTool({
    name: 'lol_watch_start',
    description: '开始向当前对话提供 LOL 数据，未配置时默认主要档、每秒采集、每15秒最多投递一批，尊重已有设置。当前回复完整显示后，根据最新状态和期间积累的事件综合决定下一句，不逐个事件排队补播。自行挑值得回应的内容，沿用已有性格。自动轮次留在轨迹；长回复按每页20字分段显示；只回复 <silent/> 则不发言。桌宠设置和 lol_tune 均可调整数据档、间隔、长短。未启动游戏时等待，重复调用不会重复监听。',
    parameters: {},
    async execute(_args, exec) { return serial(() => startNow(exec.agent)); },
  });
  addTool({
    name: 'lol_tune',
    description: '查看或调整调用者会话的陪玩设置并保存。用户要求多说/少说/快一点/慢一点/不限字数时可调用；只调传输及表达偏好，不决定游戏重点。未提供的参数不变，空参数只查询。已绑定会话立即生效，无需重启；其他会话只修改自己的设置，不影响当前监听。replyLength是软建议，气泡仍每页20字，长回复本地分页；不额外调用模型改写。',
    parameters: {
      pollIntervalMs: { type: 'number', description: '可选，采样间隔毫秒，整数>=250；默认1000。更快的本地采集不等于更多模型请求。' },
      decisionIntervalMs: { type: 'number', description: '可选，自动模型批次的最小间隔毫秒，整数>=500；默认15000。支持15000/10000/5000/2000/1000/500及自定义。越短潜在调用越多，不保证同等回复速度。当前回复完整显示且模型空闲后，间隔已到则依据最新数据开始下一次判断。若小于当前采样间隔且未指定pollIntervalMs，会同步加快采样。' },
      dataMode: { type: 'string', description: '可选：basic主要数据（阶段、模式、选人、英雄、等级、KDA、存活状态及事件）；detailed详细数据（更多安全接口字段，含血量金币装备等）。未配置时默认basic，已有设置不变。两档自动快照都省略累计事件正文，保留全部新增事件及有界过程历史。只影响后续自动采样；原有缓存最后投递一次，lol_read仍可查全量。' },
      bubbleHoldMs: { type: 'number', description: '可选，每页气泡停留毫秒，整数500..30000；默认5000，另有淡入淡出各250毫秒。下一条回复开始生效。' },
      replyLength: { type: 'number', description: '可选，建议单次陪玩回复总字数，非负整数；默认20，0表示不限制总字数偏好。不是硬上限。' },
    },
    async execute(args, exec) {
      return serial(() => {
        const result = applyPreferences(exec.agent, args);
        if (result.active) sendPreferences();
        return result;
      });
    },
  });
  addTool({
    name: 'lol_watch_stop',
    description: '停止当前对话的 LOL 数据监听。仅停止数据接入；少说话之类的交流偏好不需要停止监听。',
    parameters: {},
    async execute(_args, exec) {
      return serial(async () => {
        if (binding && binding.agent !== exec.agent) return { ...status(), stopped: false, reason: 'LISTENER_BELONGS_TO_ANOTHER_SESSION' };
        await stopNow();
        return { ...status(), stopped: true };
      });
    },
  });
  const unsubscribe = ctx.on('session/event', (session, event) => {
    if (event.type === 'turn/start') sessionTurns.set(session, event.data.turn);
    const suppressed = suppressedTurns.get(session);
    // A new human steering input can deliberately resume an otherwise stale turn.
    if (event.type === 'user/message' && !['lol-companion', 'runtime-context'].includes(event.data?.source?.kind)) {
      suppressed?.delete(sessionTurns.get(session));
    }
    const stale = suppressed?.has(event.data?.turn);
    if (event.type === 'turn/end') { suppressed?.delete(event.data.turn); sessionTurns.delete(session); }
    if (!binding || session !== binding.agent.session) return;
    const current = binding;
    if (event.type === 'turn/start') {
      current.currentTurn = event.data.turn;
      current.turns.set(event.data.turn, { automatic: false, manual: false });
      current.modelBusy = true; syncBusy(current);
    }
    if (event.type === 'user/message') {
      const state = current.turns.get(current.currentTurn);
      const kind = event.data?.source?.kind;
      if (state) {
        if (kind === 'lol-companion') state.automatic = true;
        // Only known runtime context is neutral; unknown inputs fail open as manual.
        else if (kind !== 'runtime-context') state.manual = true;
      }
      current.messageIds.delete(event.data?.id);
      current.manualMessageIds.delete(event.data?.id);
      syncBusy(current);
    }
    if (!stale) current.collector.accept(event);
    if (event.type === 'turn/end') {
      current.turns.delete(event.data.turn);
      if (typeof current.agent.whenIdle === 'function') {
        // Manual turns need this fallback too, even on hosts without agent/status events.
        const release = () => {
          if (binding === current && current.turns.size === 0) { current.modelBusy = false; syncBusy(current); }
        };
        void current.agent.whenIdle().then(release, release);
      } else { current.modelBusy = false; syncBusy(current); }
    }
  });
  const undispose = ctx.on('session/disposed', session => {
    disposedSessions.add(session);
    if (binding && session === binding.agent.session) void serial(async () => {
      if (binding && session === binding.agent.session) await stopNow();
    }).catch(() => {});
  });
  async function cleanup() {
    if (disposeTask) return disposeTask;
    disposed = true;
    disposeTask = serial(async () => {
      await stopNow();
      desktop.close();
      if (typeof unsubscribe === 'function') unsubscribe();
      if (typeof undispose === 'function') undispose();
      unsubscribePlayback?.();
      unsubscribePreferences?.();
      unsubscribeChatSubmit?.();
      unsubscribeReady?.();
      for (const unregister of registered.splice(0)) unregister();
      record();
    });
    return disposeTask;
  }
  ctx.effect(() => cleanup);
  record();
  return cleanup;
}
