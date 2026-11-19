// Transcript model (3.7). Pure; unit-tested.
import type { Message, MessagesPage, Part, SessionRow } from '../../../shared/api';

/** Merge a page into the list: messages at index >= page.from are replaced (the last one may still be streaming). */
export function mergePage(list: Message[], page: MessagesPage): Message[] {
  const keep = list.filter((m) => m.index < page.from);
  const out = [...keep, ...page.messages.filter((m) => m && typeof m.index === 'number')];
  out.sort((a, b) => a.index - b.index);
  return out.length > page.count ? out.slice(0, page.count) : out;
}
/** Where the next poll starts: re-read the last message (it may still be growing). */
export function nextFrom(list: Message[]): number { return list.length ? Math.max(0, list[list.length - 1].index) : 0; }

export type PartView = { kind: 'text'; text: string } | { kind: 'reasoning'; text: string }
  | { kind: 'tool'; name: string; status: string; input: string; output: string } | { kind: 'other'; label: string };
export function partView(p: Part): PartView | null {
  const s = (v: unknown) => (typeof v === 'string' ? v : v === undefined || v === null ? '' : JSON.stringify(v, null, 2));
  switch (p.type) {
    case 'text': return s(p.text).trim() ? { kind: 'text', text: s(p.text) } : null;
    case 'reasoning': return s(p.text).trim() ? { kind: 'reasoning', text: s(p.text) } : null;
    case 'tool': {
      const st = (p.state ?? {}) as Record<string, unknown>;
      return { kind: 'tool', name: s(p.tool) || 'tool', status: s(st.status), input: s(st.input), output: s(st.output ?? st.error) };
    }
    case 'step-start': case 'step-finish': case 'snapshot': return null;
    default: return { kind: 'other', label: s(p.type) };
  }
}
/** Sessions list: newest activity first, working ones on top, optional text filter. */
export function sortSessions(rows: SessionRow[], q = ''): SessionRow[] {
  const t = q.trim().toLowerCase();
  const f = t ? rows.filter((r) => `${r.title} ${r.id} ${r.directory} ${r.agent} ${r.model}`.toLowerCase().includes(t)) : rows;
  return [...f].sort((a, b) => Number(b.working) - Number(a.working) || (b.updated || 0) - (a.updated || 0));
}
