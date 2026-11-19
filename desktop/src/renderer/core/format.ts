// Defensive formatting of server values (ticket times arrive in whatever state.json holds). Pure; unit-tested.

/** Parse a time in any of: epoch ms, epoch s, ISO string, numeric string. Returns epoch ms or null. */
export function toMs(v: unknown): number | null {
  if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
  if (typeof v === 'number') {
    if (!Number.isFinite(v) || v <= 0) return null;
    return v < 1e11 ? Math.round(v * 1000) : Math.round(v);   // < 1e11: seconds (until year 5138)
  }
  if (typeof v === 'string') {
    const s = v.trim();
    if (/^\d+(\.\d+)?$/.test(s)) return toMs(Number(s));
    const t = Date.parse(s);
    return Number.isFinite(t) ? t : null;
  }
  return null;
}

export function ago(v: unknown, now = Date.now()): string {
  const ms = toMs(v);
  if (ms === null) return '';
  const d = Math.max(0, now - ms) / 1000;
  if (d < 45) return 'just now';
  if (d < 90) return '1 min ago';
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  if (d < 86400 * 30) return `${Math.round(d / 86400)} d ago`;
  return new Date(ms).toISOString().slice(0, 10);
}

export function clock(v: unknown): string {
  const ms = toMs(v);
  if (ms === null) return '';
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function dateTime(v: unknown): string {
  const ms = toMs(v);
  if (ms === null) return '';
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${clock(ms)}`;
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return '';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, '0')}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${n} B`;
  const u = ['KiB', 'MiB', 'GiB', 'TiB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
}

export function tokens(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '';
  if (n < 1000) return String(Math.round(n));
  if (n < 1e6) return `${(n / 1000).toFixed(n < 1e4 ? 1 : 0)}k`;
  return `${(n / 1e6).toFixed(n < 1e7 ? 2 : 1)}M`;
}

export function cost(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '';
  if (n === 0) return '$0';
  if (n < 0.01) return '<$0.01';
  return `$${n < 100 ? n.toFixed(2) : Math.round(n)}`;
}

/** Days until a timestamp, rounded down; negative when past. */
export function daysUntil(ms: number | null, now = Date.now()): number | null {
  if (ms === null || !Number.isFinite(ms)) return null;
  return Math.floor((ms - now) / 86400000);
}

export function basename(p: string): string {
  const s = p.replace(/[\\/]+$/, '');
  const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  return i >= 0 ? s.slice(i + 1) || s : s;
}

export function plural(n: number, one: string, many = one + 's'): string { return `${n} ${n === 1 ? one : many}`; }
