// Structural transport compression only: no game fields receive special importance.
// Paths are arrays, so arbitrary JSON field names do not need pointer escaping.
export function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}

export function observationKey(observation) {
  const { capturedAt, ...state } = observation;
  return JSON.stringify(canonical(state));
}

/** Produce operations that transform current into previous, without mutating either. */
export function reverseDelta(current, previous, path = []) {
  if (Object.is(current, previous)) return [];
  const currentObject = current !== null && typeof current === 'object';
  const previousObject = previous !== null && typeof previous === 'object';
  if (!currentObject || !previousObject || Array.isArray(current) !== Array.isArray(previous)) {
    return [{ op: 'set', path, value: structuredClone(previous) }];
  }
  if (Array.isArray(current)) {
    const operations = [];
    for (let index = 0; index < previous.length; index++) {
      if (index >= current.length) operations.push({ op: 'set', path: [...path, index], value: structuredClone(previous[index]) });
      else operations.push(...reverseDelta(current[index], previous[index], [...path, index]));
    }
    // Extend with actual values before changing length, so ordinary JSON arrays
    // never acquire holes. Shrinking removes only the surplus tail. The string
    // path component "length" denotes an array's length, not an element.
    if (current.length !== previous.length) operations.push({ op: 'set', path: [...path, 'length'], value: previous.length });
    return operations;
  }
  const operations = [];
  for (const key of Object.keys(current).sort()) {
    if (!Object.hasOwn(previous, key)) operations.push({ op: 'delete', path: [...path, key] });
  }
  for (const key of Object.keys(previous).sort()) {
    if (!Object.hasOwn(current, key)) operations.push({ op: 'set', path: [...path, key], value: structuredClone(previous[key]) });
    else operations.push(...reverseDelta(current[key], previous[key], [...path, key]));
  }
  return operations;
}
