const PRIVATE_FIELD = /(?:password|passwd|token|authorization|credential|secret|privatekey|apikey|voice|jwt|auth(?:entication)?(?:key|data|header)?$)/i;

/** Remove transport credentials and voice-room credentials before handing data to a model. */
export function sanitizeData(value, seen = new WeakSet()) {
  if (value === null || typeof value !== 'object') return value;
  if (seen.has(value)) return '[circular]';
  seen.add(value);
  const result = Array.isArray(value) ? [] : {};
  for (const [key, child] of Object.entries(value)) {
    if (PRIVATE_FIELD.test(key)) continue;
    // JSON may legally contain __proto__. Define an own data property instead
    // of invoking Object.prototype's legacy setter on this ordinary object.
    Object.defineProperty(result, key, {
      value: sanitizeData(child, seen), enumerable: true, writable: true, configurable: true,
    });
  }
  seen.delete(value);
  return result;
}

export function safeReason(error) {
  if (error?.name === 'AbortError') return 'cancelled';
  const code = String(error?.code || '');
  if (/CERT|TLS|SELF_SIGNED|UNABLE_TO_VERIFY|DEPTH_ZERO/.test(code)) return 'tls_verification_failed';
  if (code === 'ETIMEDOUT') return 'request_timeout';
  if (code === 'ECONNREFUSED') return 'not_listening';
  if (code === 'ECONNRESET') return 'connection_reset';
  if (code === 'EJSON') return 'invalid_json';
  if (code === 'ETOOBIG') return 'response_too_large';
  return 'request_failed';
}

export function aborted() {
  const error = new Error('Operation cancelled');
  error.name = 'AbortError';
  return error;
}

export function checkAbort(signal) {
  if (signal?.aborted) throw aborted();
}
