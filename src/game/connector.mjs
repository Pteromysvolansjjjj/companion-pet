import { createCredentialDiscovery } from './discovery.mjs';
import { createHttpsTransport, LCU_ENDPOINTS, LIVE_ENDPOINT } from './transport.mjs';
import { sanitizeData, safeReason, checkAbort } from './sanitize.mjs';

function identityNames(player) {
  return [player?.riotId, player?.summonerName, player?.displayName,
    player?.riotIdGameName && player?.riotIdTagLine ? `${player.riotIdGameName}#${player.riotIdTagLine}` : null]
    .filter(Boolean).map(value => String(value).toLowerCase());
}

function identifySelf(sections) {
  const active = sections.live?.data?.activePlayer;
  const profile = sections.summoner?.data;
  const names = new Set([...identityNames(active), ...identityNames(profile)]);
  const players = sections.live?.data?.allPlayers ?? [];
  const candidates = players.filter(player =>
    (profile?.puuid && player.puuid === profile.puuid) || identityNames(player).some(name => names.has(name)));
  return {
    status: candidates.length === 1 ? 'matched' : 'unresolved',
    ...(candidates.length === 1 ? { player: candidates[0] } : {}),
    ...(active ? { activePlayer: active } : {}),
    ...(profile ? { account: profile } : {}),
  };
}

export function createLeagueConnector(options = {}) {
  const discovery = options.discovery ?? createCredentialDiscovery(options);
  const transport = options.transport ?? createHttpsTransport(options);
  let queue = Promise.resolve();
  let latestSnapshot = null;

  async function sectionRequest(port, endpoint, password, signal) {
    try {
      checkAbort(signal);
      const result = await transport.request({ port, path: endpoint, password, signal });
      checkAbort(signal);
      if (result.status === 404 || result.status === 204) return { status: 'unavailable', reason: 'not_available_in_current_state' };
      if (result.status >= 200 && result.status < 300) return { status: 'ok', data: sanitizeData(result.data) };
      if (result.status === 401 || result.status === 403) return { status: 'error', reason: 'authentication_failed' };
      return { status: 'error', reason: 'http_error', httpStatus: result.status };
    } catch (error) {
      checkAbort(signal);
      return { status: 'error', reason: safeReason(error) };
    }
  }

  async function collect(signal) {
    checkAbort(signal);
    const sections = {};
    let credential;
    try { credential = await discovery.discover({ signal }); } catch { checkAbort(signal); }
    const sources = { lcu: { available: false }, live: { available: false } };
    if (credential) {
      const phase = await sectionRequest(credential.port, LCU_ENDPOINTS.phase, credential.password, signal);
      sections.phase = phase;
      if (phase.status === 'error') {
        if (phase.reason === 'authentication_failed' || phase.reason === 'not_listening') discovery.invalidate?.();
        // A failed connection probe must not multiply its timeout by every endpoint.
        for (const name of Object.keys(LCU_ENDPOINTS)) sections[name] ??= { status: 'unavailable', reason: phase.reason };
      } else {
        const entries = Object.entries(LCU_ENDPOINTS).filter(([name]) => name !== 'phase');
        // Independent endpoints use a small bounded pool; complete snapshots never overlap.
        const parallelism = Math.max(1, Math.min(4, Number(options.lcuConcurrency) || 3));
        for (let index = 0; index < entries.length; index += parallelism) {
          const results = await Promise.all(entries.slice(index, index + parallelism).map(async ([name, endpoint]) =>
            [name, await sectionRequest(credential.port, endpoint, credential.password, signal)]));
          for (const [name, result] of results) {
            sections[name] = result;
            if (result.reason === 'authentication_failed') discovery.invalidate?.();
          }
        }
      }
      sources.lcu = { available: Object.values(sections).some(section => section.status === 'ok'),
        ...(sections.phase?.status !== 'ok' ? { reason: sections.phase?.reason ?? 'data_unavailable' } : {}) };
    } else {
      for (const name of Object.keys(LCU_ENDPOINTS)) sections[name] = { status: 'unavailable', reason: 'client_credentials_not_found' };
      sources.lcu.reason = 'client_credentials_not_found';
    }
    sections.live = await sectionRequest(2999, LIVE_ENDPOINT, undefined, signal);
    sources.live = { available: sections.live.status === 'ok', ...(sections.live.reason ? { reason: sections.live.reason } : {}) };
    checkAbort(signal);
    const snapshot = { capturedAt: new Date().toISOString(), sources, sections, self: identifySelf(sections) };
    latestSnapshot = snapshot;
    return snapshot;
  }

  return {
    /** All reads are serialized, including reads requested while a watcher is running. */
    read({ signal } = {}) {
      const work = queue.then(() => collect(signal));
      queue = work.catch(() => {});
      return work;
    },
    get latestSnapshot() { return latestSnapshot; },
  };
}
