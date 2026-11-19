// Atomic JSON files with corrupt-file recovery (D12). Pure node; unit-tested.
import { readFileSync, writeFileSync, renameSync, mkdirSync, existsSync, fsyncSync, openSync, closeSync } from 'node:fs';
import { dirname } from 'node:path';

export interface LoadResult<T> { value: T; recovered: string | null }

/** Read JSON; a missing file gives `fallback()`, an unparsable one is renamed aside (`<file>.corrupt-<ms>`). */
export function loadJson<T>(file: string, fallback: () => T, coerce: (raw: unknown) => T | null): LoadResult<T> {
  if (!existsSync(file)) return { value: fallback(), recovered: null };
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    return { value: fallback(), recovered: moveAside(file) };
  }
  const v = coerce(raw);
  if (v === null) return { value: fallback(), recovered: moveAside(file) };
  return { value: v, recovered: null };
}

function moveAside(file: string): string {
  const aside = `${file}.corrupt-${Date.now()}`;
  try { renameSync(file, aside); } catch { /* best effort */ }
  return aside;
}

/** Write via a temp file + fsync + rename, so a crash never leaves a half-written file. */
export function saveJson(file: string, value: unknown, mode = 0o600): void {
  mkdirSync(dirname(file), { recursive: true });
  const tmp = `${file}.tmp-${process.pid}`;
  writeFileSync(tmp, JSON.stringify(value, null, 2) + '\n', { mode });
  try { const fd = openSync(tmp, 'r+'); fsyncSync(fd); closeSync(fd); } catch { /* fsync best effort */ }
  renameSync(tmp, file);
}

export function isObj(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

/**
 * Merge `patch` onto `base` following the shape of `base`: unknown keys are dropped, a value whose type
 * differs from the base value is dropped, objects recurse. Keys whose base value is null accept any
 * scalar (string/number) or null. `free` names paths (dot-joined) that are free-form records.
 */
export function shapeMerge<T>(base: T, patch: unknown, free: Set<string> = new Set(), at = ''): T {
  if (!isObj(patch) || !isObj(base)) return base;
  const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
  for (const [k, pv] of Object.entries(patch)) {
    if (!(k in out)) continue;
    const path = at ? `${at}.${k}` : k;
    const bv = out[k];
    if (free.has(path)) { if (isObj(pv)) out[k] = pv; continue; }
    if (bv === null) { if (pv === null || typeof pv === 'string' || typeof pv === 'number') out[k] = pv; continue; }
    if (isObj(bv)) { out[k] = shapeMerge(bv, pv, free, path); continue; }
    if (Array.isArray(bv)) { if (Array.isArray(pv)) out[k] = pv; continue; }
    if (pv === null && path.endsWith('installRoot')) { out[k] = null; continue; }
    if (typeof pv === typeof bv) out[k] = pv;
  }
  return out as T;
}
