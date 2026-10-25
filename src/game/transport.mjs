import https from 'node:https';
import tls from 'node:tls';
import { X509Certificate, timingSafeEqual } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { checkAbort } from './sanitize.mjs';

export const LCU_ENDPOINTS = Object.freeze({
  phase: '/lol-gameflow/v1/gameflow-phase',
  summoner: '/lol-summoner/v1/current-summoner',
  lobby: '/lol-lobby/v2/lobby',
  matchmaking: '/lol-matchmaking/v1/search',
  readyCheck: '/lol-matchmaking/v1/ready-check',
  champSelect: '/lol-champ-select/v1/session',
  endgame: '/lol-end-of-game/v1/eog-stats-block',
  gameflow: '/lol-gameflow/v1/session',
});
export const LIVE_ENDPOINT = '/liveclientdata/allgamedata';
const ALLOWED = new Set([...Object.values(LCU_ENDPOINTS), LIVE_ENDPOINT]);

export function createHttpsTransport(options = {}) {
  let caPromise;
  const getCA = () => options.ca ?? (options.caPath ? caPromise ??= readFile(options.caPath) : undefined);

  return {
    async request({ port, path, password, signal }) {
      checkAbort(signal);
      if (!ALLOWED.has(path)) throw new Error('Endpoint not permitted');
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid local port');
      const ca = await getCA();
      checkAbort(signal);
      return new Promise((resolve, reject) => {
        // Always use local HTTPS, GET, verified CA and identity; never disable TLS checks.
        const req = https.request({
          hostname: '127.0.0.1', port, path, method: 'GET',
          ca, rejectUnauthorized: true, signal, agent: false,
          checkServerIdentity(hostname, cert) {
            // Riot's local certificate may use DNS localhost instead of IP SAN.
            const identityError = tls.checkServerIdentity('localhost', cert);
            if (identityError) return identityError;
            if (options.certificateSha256) {
              const actual = new X509Certificate(cert.raw).fingerprint256.replaceAll(':', '').toLowerCase();
              const expected = String(options.certificateSha256).replaceAll(':', '').toLowerCase();
              if (actual.length !== expected.length || !timingSafeEqual(Buffer.from(actual), Buffer.from(expected))) {
                const error = new Error('Local certificate pin mismatch'); error.code = 'ERR_TLS_CERT_PIN_MISMATCH'; return error;
              }
            }
          },
          headers: { Accept: 'application/json', ...(password ? { Authorization: `Basic ${Buffer.from(`riot:${password}`).toString('base64')}` } : {}) },
        }, response => {
          let size = 0;
          const chunks = [];
          response.on('data', chunk => {
            size += chunk.length;
            if (size > (options.maxResponseBytes ?? 8 * 1024 * 1024)) {
              const error = new Error('Local response too large'); error.code = 'ETOOBIG'; req.destroy(error); return;
            }
            chunks.push(chunk);
          });
          response.on('error', reject);
          response.on('end', () => {
            const status = response.statusCode ?? 0;
            // Never propagate error bodies, which may contain sensitive implementation details.
            if (status === 204 || status < 200 || status >= 300) { resolve({ status }); return; }
            try { resolve({ status, data: JSON.parse(Buffer.concat(chunks).toString('utf8')) }); }
            catch { const error = new Error('Invalid local JSON'); error.code = 'EJSON'; reject(error); }
          });
        });
        req.setTimeout(options.requestTimeoutMs ?? 2500, () => {
          const error = new Error('Local request timed out'); error.code = 'ETIMEDOUT'; req.destroy(error);
        });
        req.on('error', reject);
        req.end();
      });
    },
  };
}
