// Command registry (PLAN W17, DR (d)): every command is registered once; palette, menus, shortcuts and the
// Keyboard settings list read from here. Pure; unit-tested.
export type CommandGroup = 'Actions' | 'Views' | 'Sessions' | 'Tickets' | 'Settings' | 'Backgrounds' | 'Layout' | 'Recent';
export interface RunCtx { split?: boolean; source?: 'palette' | 'key' | 'menu' | 'click' }
export interface Command {
  id: string; title: string; group: CommandGroup;
  keywords?: string[]; chord?: string;          // default chord(s), comma-separated
  hint?: string;                                // dimmed context in the palette
  when?: () => boolean;                         // hidden from palette and ignored by keys when false
  /** Single-letter view shortcuts: active only while this view's panel is focused. */
  scope?: string;
  run: (ctx: RunCtx) => void | Promise<void>;
}

export class CommandRegistry {
  private map = new Map<string, Command>();
  private listeners = new Set<() => void>();
  version = 0;

  register(c: Command | Command[]): () => void {
    const list = Array.isArray(c) ? c : [c];
    for (const x of list) this.map.set(x.id, x);
    this.bump();
    return () => { for (const x of list) if (this.map.get(x.id) === x) this.map.delete(x.id); this.bump(); };
  }
  get(id: string): Command | undefined { return this.map.get(id); }
  all(): Command[] { return [...this.map.values()]; }
  available(): Command[] { return this.all().filter((c) => !c.when || safe(c.when)); }
  async run(id: string, ctx: RunCtx = {}): Promise<boolean> {
    const c = this.map.get(id);
    if (!c || (c.when && !safe(c.when))) return false;
    await c.run(ctx);
    return true;
  }
  onChange(cb: () => void): () => void { this.listeners.add(cb); return () => this.listeners.delete(cb); }
  private bump() { this.version++; for (const l of this.listeners) l(); }
}
function safe(f: () => boolean): boolean { try { return f(); } catch { return false; } }

/** Fuzzy subsequence score (higher is better; -1 = no match). Bonuses for word starts and runs. */
export function fuzzyScore(query: string, text: string): number {
  const q = query.toLowerCase().trim();
  if (!q) return 0;
  const t = text.toLowerCase();
  const direct = t.indexOf(q);
  if (direct >= 0) return 1000 - direct + (direct === 0 || /[\s\-_:./]/.test(t[direct - 1]) ? 200 : 0) - t.length * 0.1;
  let score = 0, ti = 0, run = 0;
  for (let qi = 0; qi < q.length; qi++) {
    const ch = q[qi];
    if (ch === ' ') continue;
    let found = -1;
    for (; ti < t.length; ti++) if (t[ti] === ch) { found = ti; break; }
    if (found < 0) return -1;
    const wordStart = found === 0 || /[\s\-_:./]/.test(t[found - 1]);
    run = found > 0 && qi > 0 && t[found - 1] === q[qi - 1] ? run + 1 : 0;
    score += 10 + (wordStart ? 25 : 0) + run * 8 - Math.min(found, 30) * 0.2;
    ti = found + 1;
  }
  return score - t.length * 0.1;
}

export interface Ranked<T> { item: T; score: number }
/** Rank items by best of title/keywords, plus a recency boost (index in `recent`, most recent first). */
export function rank<T extends { id: string; title: string; keywords?: string[] }>(items: T[], query: string, recent: string[] = []): Ranked<T>[] {
  const out: Ranked<T>[] = [];
  for (const item of items) {
    let s = fuzzyScore(query, item.title);
    for (const k of item.keywords ?? []) s = Math.max(s, fuzzyScore(query, k) - 5);
    if (s < 0) continue;
    const r = recent.indexOf(item.id);
    if (r >= 0) s += Math.max(0, 60 - r * 6);
    out.push({ item, score: s });
  }
  return out.sort((a, b) => b.score - a.score || a.item.title.localeCompare(b.item.title));
}

/** Palette prefixes (W17): '>' commands, '#' tickets, '@' sessions, '/' vault files. */
export function parsePrefix(input: string): { mode: 'all' | 'commands' | 'tickets' | 'sessions' | 'files'; query: string } {
  const c = input[0];
  if (c === '>') return { mode: 'commands', query: input.slice(1).trim() };
  if (c === '#') return { mode: 'tickets', query: input.slice(1).trim() };
  if (c === '@') return { mode: 'sessions', query: input.slice(1).trim() };
  if (c === '/') return { mode: 'files', query: input.slice(1).trim() };
  return { mode: 'all', query: input.trim() };
}

/** Recency list update: move id to the front, cap length. */
export function pushRecent(list: string[], id: string, cap = 30): string[] {
  return [id, ...list.filter((x) => x !== id)].slice(0, cap);
}
