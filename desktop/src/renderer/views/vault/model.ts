// Vault view model: page filter, folder-tree flattening (for a windowed list), in-vault link resolution (relative
// Markdown links, [[wiki links]], images -> "#vault:<rel>" that the file panel opens in-app) and ingest status.
// Pure; unit-tested in model.test.ts.

export interface PageRow { rel: string; title: string; mtime?: number }
export interface TreeEntry { name: string; path: string; dir: boolean; size: number }
export interface FlatRow { e: TreeEntry; depth: number; open: boolean; loading: boolean }

/** Pages filtered by a case-insensitive query on title or path, sorted by path. Fast enough per keystroke at 10k. */
export function filterPages(pages: PageRow[], q: string): PageRow[] {
  const s = q.trim().toLowerCase();
  const out = s ? pages.filter((p) => p.rel.toLowerCase().includes(s) || p.title.toLowerCase().includes(s)) : pages.slice();
  return out.sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
}

/** Expanded folders' children, depth first, folders before files (server order otherwise). */
export function flattenTree(kids: Record<string, TreeEntry[] | undefined>, open: Set<string>, loading: Set<string> = new Set(), root = ''): FlatRow[] {
  const out: FlatRow[] = [];
  const walk = (dir: string, depth: number) => {
    const list = kids[dir];
    if (!list) return;
    const sorted = [...list].sort((a, b) => (a.dir === b.dir ? 0 : a.dir ? -1 : 1));
    for (const e of sorted) {
      const isOpen = e.dir && open.has(e.path);
      out.push({ e, depth, open: isOpen, loading: loading.has(e.path) });
      if (isOpen && depth < 64) walk(e.path, depth + 1);
    }
  };
  walk(root, 0);
  return out;
}

/** A vault-relative path from a link in page `from`, or null (external, absolute outside, escapes the vault). */
export function resolveRel(from: string, href: string): string | null {
  let h = href.trim();
  if (!h || /^[a-z][a-z0-9+.-]*:/i.test(h) || h.startsWith('#') || h.startsWith('//')) return null;
  h = h.replace(/[?#].*$/, '');
  try { h = decodeURIComponent(h); } catch { /* keep as is */ }
  if (!h) return null;
  const base = h.startsWith('/') ? [] : from.split('/').slice(0, -1);
  const parts = [...base];
  for (const seg of h.split(/[\\/]+/)) {
    if (!seg || seg === '.') continue;
    if (seg === '..') { if (!parts.length) return null; parts.pop(); continue; }
    parts.push(seg);
  }
  if (!parts.length || parts[0] === '.git') return null;
  return parts.join('/');
}

/** The page a [[wiki link]] names: exact path (with or without .md), else a unique basename match (case-insensitive). */
export function wikiTarget(name: string, pages: PageRow[]): string | null {
  const n = name.trim().replace(/#.*$/, '').replace(/^\/+/, '');
  if (!n) return null;
  const want = n.toLowerCase().replace(/\.md$/, '');
  let hit: string | null = null;
  for (const p of pages) {
    const rel = p.rel.toLowerCase().replace(/\.md$/, '');
    if (rel === want) return p.rel;
    const base = rel.slice(rel.lastIndexOf('/') + 1);
    if (base === want && hit === null) hit = p.rel;
  }
  return hit;
}

export const VAULT_HREF = '#vault:';
export function vaultHref(rel: string): string { return VAULT_HREF + encodeURIComponent(rel); }
export function parseVaultHref(href: string | null | undefined): string | null {
  if (!href || !href.startsWith(VAULT_HREF)) return null;
  let rel: string;
  try { rel = decodeURIComponent(href.slice(VAULT_HREF.length)); } catch { return null; }
  // a hand-written #vault: link skips resolveRel: still no absolute path, no `..`/`.` segment, nothing under .git
  const segs = rel.split(/[\\/]/);
  if (!rel || rel.startsWith('/') || segs.some((s) => !s || s === '..' || s === '.') || segs[0] === '.git' || rel.includes('\0')) return null;
  return rel;
}

const IMG_RE = /\.(png|jpe?g|gif|webp|svg)$/i;
export function isImage(rel: string): boolean { return IMG_RE.test(rel); }

/**
 * Rewrite in-vault links of a Markdown page so they stay in the app: [t](rel), ![alt](img) (the one Markdown renderer
 * drops images, so they become links) and [[Page|alias]]. Code spans and fenced blocks are left alone.
 */
export function linkify(text: string, from: string, pages: PageRow[]): string {
  const parts = text.split(/(^```[\s\S]*?^```[^\n]*$|^~~~[\s\S]*?^~~~[^\n]*$|`[^`\n]+`)/m);
  // split with a capture group: odd indexes are code; the m flag makes ^ match line starts
  return parts.map((p, i) => (i % 2 ? p : rewrite(p, from, pages))).join('');
}
function rewrite(s: string, from: string, pages: PageRow[]): string {
  s = s.replace(/(!?)\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"\n]*")?\s*\)/g, (m, bang: string, label: string, href: string) => {
    const rel = resolveRel(from, href);
    if (!rel) return bang ? `[${label || href}](${href})` : m;
    const text = bang ? `image: ${label || rel.slice(rel.lastIndexOf('/') + 1)}` : label;
    return `[${text}](${vaultHref(rel)})`;
  });
  return s.replace(/(!?)\[\[([^\]\n|]+)(?:\|([^\]\n]*))?\]\]/g, (m, _bang: string, name: string, alias?: string) => {
    const rel = wikiTarget(name, pages) ?? (isImage(name) ? resolveRel(from, name) : null);
    const label = (alias ?? name).replace(/[[\]]/g, '');
    return rel ? `[${label}](${vaultHref(rel)})` : label;
  });
}

// ---------------------------------------------------------------------------------------------- ingest
export interface IngestStatus { running: boolean; stale: boolean; label: string; tone: 'ok' | 'warn' | 'err' | 'info' | 'default' }
export function ingestStatus(ing: { run: Record<string, unknown> | null; summary: unknown } | null | undefined): IngestStatus {
  const run = ing?.run;
  const sum = (ing?.summary && typeof ing.summary === 'object' ? ing.summary : {}) as Record<string, unknown>;
  const nfailed = typeof sum.nfailed === 'number' ? sum.nfailed : 0;
  if (run && run.alive === true) return { running: true, stale: false, label: 'Ingest running', tone: 'ok' };
  if (run && run.alive === false) return { running: false, stale: true, label: 'Ingest stopped (no heartbeat)', tone: 'warn' };
  if (nfailed) return { running: false, stale: false, label: `${nfailed} unit${nfailed === 1 ? '' : 's'} failed`, tone: 'err' };
  if (sum.has) return { running: false, stale: false, label: 'Ingested', tone: 'info' };
  return { running: false, stale: false, label: 'Not ingested yet', tone: 'default' };
}
export interface IngestSource { name: string; path: string; exists: boolean; ok: number; failed: number; pages: number; at: string }
export function ingestSources(summary: unknown): IngestSource[] {
  const s = summary && typeof summary === 'object' ? (summary as Record<string, unknown>).sources : null;
  return Array.isArray(s) ? s.filter((x) => x && typeof x === 'object').map((x) => {
    const o = x as Record<string, unknown>;
    return { name: String(o.name ?? ''), path: String(o.path ?? ''), exists: o.exists !== false, ok: Number(o.ok) || 0,
      failed: Number(o.failed) || 0, pages: Number(o.pages) || 0, at: String(o.at ?? '') };
  }) : [];
}
export interface IngestFailure { id: string; page: string; attempts: number; at: string; problems: string[] }
export function ingestFailures(summary: unknown): IngestFailure[] {
  const s = summary && typeof summary === 'object' ? (summary as Record<string, unknown>).failed : null;
  return Array.isArray(s) ? s.filter((x) => x && typeof x === 'object').map((x) => {
    const o = x as Record<string, unknown>;
    return { id: String(o.id ?? ''), page: String(o.page ?? ''), attempts: Number(o.attempts) || 0, at: String(o.at ?? ''),
      problems: Array.isArray(o.problems) ? o.problems.map(String) : [] };
  }) : [];
}

/** /vault/file/<rel> path for api.bytes, each segment encoded. */
export function bytesPath(rel: string): string { return '/vault/file/' + rel.split('/').map(encodeURIComponent).join('/'); }
