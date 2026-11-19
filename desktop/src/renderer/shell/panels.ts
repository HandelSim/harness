// Panel kinds, ids, params validation and the reopen stack (PLAN 3.2, W4, W20). Pure; unit-tested.
export const VIEW_IDS = ['home', 'queue', 'sessions', 'findings', 'vault', 'jira', 'debug', 'keys', 'updates', 'settings'] as const;
export type ViewId = typeof VIEW_IDS[number];
export const VIEW_TITLES: Record<ViewId, string> = {
  home: 'Home', queue: 'Queue', sessions: 'Sessions', findings: 'Findings', vault: 'Vault', jira: 'Jira', debug: 'Debug',
  keys: 'Keys', updates: 'Updates', settings: 'Settings',
};
/** Serve page each native view falls back to (parity matrix 3.16). */
// Only GET pages serve/pub.py routes: findings and Jira are panels of the queue page (anchors), there is no
// /runs/findings or Jira page (/runs/queue/jira is the panel's JSON), and /settings/system is POST-only.
export const VIEW_WEB: Record<ViewId, string> = {
  home: '/', queue: '/runs/queue', sessions: '/runs', findings: '/runs/queue#findings', vault: '/vault', jira: '/runs/queue#jira',
  debug: '/runs/debug', keys: '/settings', updates: '/settings', settings: '/settings',
};
const seg = (rel: string) => rel.split('/').map(encodeURIComponent).join('/');
/** Web pages for one thing (serve/pub.py runs_get / vault_get). */
export const WEB = {
  session: (sid: string) => `/runs/${encodeURIComponent(sid)}`,
  debugReport: (kind: string, name: string) => `/runs/debug/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`,
  vaultFile: (rel: string) => `/vault/browse/${seg(rel)}`,
  findingsAgent: '/runs/queue#findings-agent',
  findingsNotes: '/runs/findings/notes',
};

export type PanelKind = 'view' | 'terminal' | 'chat' | 'transcript' | 'ticket' | 'diff' | 'web' | 'vault-file';
export interface PanelRef { kind: PanelKind; key: string }

/** Panel id from kind + instance key. Views: "view.<id>"; others "<kind>:<key>". */
export function panelId(kind: PanelKind, key: string): string {
  return kind === 'view' ? `view.${key}` : `${kind}:${key}`;
}
export function parsePanelId(id: string): PanelRef | null {
  if (id.startsWith('view.')) {
    const v = id.slice(5);
    return (VIEW_IDS as readonly string[]).includes(v) ? { kind: 'view', key: v } : null;
  }
  const i = id.indexOf(':');
  if (i <= 0) return null;
  const kind = id.slice(0, i) as PanelKind;
  if (!['terminal', 'chat', 'transcript', 'ticket', 'diff', 'web', 'vault-file'].includes(kind)) return null;
  const key = id.slice(i + 1);
  return key ? { kind, key } : null;
}
/** Singleton per instance key: views, transcript (per sid), ticket (per key), diff (per tuple), vault-file. */
export function isKeyed(kind: PanelKind): boolean { return kind !== 'terminal' && kind !== 'chat' && kind !== 'web'; }

const str = (v: unknown, max = 4096) => typeof v === 'string' && v.length > 0 && v.length <= max;
const TICKET_RE = /^[A-Za-z0-9._-]{1,80}$/;
const SID_RE = /^[A-Za-z0-9._-]{1,128}$/;

/** Validate restored params for a kind; null = drop the panel (W20: never fatal). */
export function validateParams(kind: PanelKind, p: unknown): Record<string, unknown> | null {
  const o = (p && typeof p === 'object' && !Array.isArray(p) ? p : {}) as Record<string, unknown>;
  switch (kind) {
    case 'view': return o;
    case 'ticket': return str(o.key, 80) && TICKET_RE.test(o.key as string) ? o : null;
    case 'transcript': return str(o.sid, 128) && SID_RE.test(o.sid as string) ? o : null;
    case 'web': return str(o.path, 2048) && (o.path as string).startsWith('/') && !(o.path as string).startsWith('//') ? o : null;
    case 'chat': return o.sid === undefined || (str(o.sid, 128) && SID_RE.test(o.sid as string)) ? o : null;
    case 'diff': return str(o.key, 80) && TICKET_RE.test(o.key as string) ? o : null;
    case 'vault-file': return str(o.path, 2048) ? o : null;
    case 'terminal': {
      const s = o.spec as Record<string, unknown> | undefined;
      if (!s || typeof s !== 'object') return null;
      if (!['agent', 'host', 'shell', 'plain'].includes(s.kind as string)) return null;
      if (!str(s.cwd, 4096)) return null;
      return o;
    }
  }
  return null;
}

/** Closed-tab stack (W4 step 5), newest last, capped. */
export class ReopenStack<T = { id: string; kind: PanelKind; params: Record<string, unknown>; title: string }> {
  constructor(public items: T[] = [], private cap = 20) { this.items = items.slice(-cap); }
  push(x: T): void { this.items.push(x); if (this.items.length > this.cap) this.items.splice(0, this.items.length - this.cap); }
  pop(): T | undefined { return this.items.pop(); }
  get size(): number { return this.items.length; }
}

/** Bounded snapshot stack for layout undo (W4 step 6). Consecutive duplicates are not stored. */
export class SnapshotStack {
  private items: string[] = [];
  constructor(private cap = 20) {}
  push(json: string): void {
    if (this.items[this.items.length - 1] === json) return;
    this.items.push(json);
    if (this.items.length > this.cap + 1) this.items.shift();
  }
  /** The snapshot before the current one (current is dropped). */
  undo(): string | undefined {
    if (this.items.length < 2) return undefined;
    this.items.pop();
    return this.items[this.items.length - 1];
  }
  get size(): number { return this.items.length; }
}

let seq = 0;
/** Unique instance key for non-keyed panels. */
export function uid(prefix = ''): string { seq = (seq + 1) % 1e6; return `${prefix}${Date.now().toString(36)}${seq.toString(36)}`; }
