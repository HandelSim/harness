// Command palette (W17): Mod+K. Prefixes: > commands, # tickets, @ sessions. Fuzzy-ranked, recents first.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'preact/hooks';
import { paletteOpen } from './ui';
import { commands, recordRecent } from './commands';
import { rank, parsePrefix, type Command } from '../core/commands';
import { formatChord } from '../core/keymap';
import { appState, platform, store } from '../core/app';
import { openPanel } from './dock';
import type { Queue, SessionRow } from '../../shared/api';

interface Item { id: string; title: string; group: string; hint?: string; chord?: string; keywords?: string[]; run: () => void }

function dynamicItems(mode: string): Item[] {
  const out: Item[] = [];
  if (mode === 'all' || mode === 'tickets') {
    const q = store.entry('queue').data.value as Queue | undefined;
    for (const t of q?.tickets ?? []) out.push({ id: `ticket:${t.key}`, title: `${t.key} ${t.title || ''}`.trim(), group: 'Tickets', hint: t.status,
      run: () => { openPanel('ticket', t.key, { title: t.key, params: { key: t.key } }); } });
  }
  if (mode === 'all' || mode === 'sessions') {
    const s = store.entry('sessions').data.value as { rows?: SessionRow[] } | SessionRow[] | undefined;
    const rows = Array.isArray(s) ? s : s?.rows ?? [];
    for (const r of rows.slice(0, 200)) {
      const sid = (r as { id?: string; sid?: string }).id ?? (r as { sid?: string }).sid ?? '';
      if (!sid) continue;
      const title = (r as { title?: string }).title || sid;
      out.push({ id: `session:${sid}`, title, group: 'Sessions', hint: sid.slice(0, 12),
        run: () => { openPanel('transcript', sid, { title: title.slice(0, 40), params: { sid } }); } });
    }
  }
  return out;
}

export function Palette() {
  const open = paletteOpen.value;
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const prevFocus = useRef<Element | null>(null);
  // layout effect, not useEffect: a post-paint reset of q could clobber text typed right after opening
  useLayoutEffect(() => {
    if (open) { prevFocus.current = document.activeElement; setQ(open.prefix); setSel(0); setTimeout(() => input.current?.focus(), 0); }
  }, [!!open]);
  const compute = (qv: string): Item[] => {
    const { mode, query } = parsePrefix(qv);
    const cmds: Item[] = mode === 'all' || mode === 'commands'
      ? commands.available().map((c: Command) => ({ id: c.id, title: c.title, group: c.group, hint: c.hint, chord: c.chord?.split(',')[0].trim(), keywords: c.keywords,
        run: () => { recordRecent(c.id); void commands.run(c.id, { source: 'palette' }); } }))
      : [];
    const all = [...cmds, ...(query || mode !== 'all' ? dynamicItems(mode) : [])];
    return rank(all, query, appState.value?.recents?.commands ?? []).slice(0, 80).map((r) => r.item);
  };
  const items = useMemo(() => (open ? compute(q) : []), [q, !!open, commands.version]);
  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }); }, [sel]);
  if (!open) return null;
  const close = () => { paletteOpen.value = false; (prevFocus.current as HTMLElement | null)?.focus?.(); };
  const runAt = (i: number) => { const it = items[i]; if (!it) return; paletteOpen.value = false; it.run(); };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(items.length - 1, s + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      // the input can be ahead of the last render (fast type + Enter): rank what is actually typed
      const typed = input.current?.value ?? q;
      if (typed !== q) { const fresh = compute(typed); paletteOpen.value = false; fresh[0]?.run(); }
      else runAt(sel);
    }
    e.stopPropagation();
  };
  let lastGroup = '';
  return (
    <>
      <div class="scrim" style={{ background: 'transparent' }} onClick={close} />
      <div class="palette glass glass--overlay" data-testid="palette" role="dialog" aria-label="Command palette">
        <input ref={input} class="palette-input" data-testid="palette-input" placeholder="Type a command, # ticket, @ session…" value={q}
          onInput={(e) => { setQ((e.target as HTMLInputElement).value); setSel(0); }} onKeyDown={onKey}
          role="combobox" aria-expanded="true" aria-controls="palette-list" aria-activedescendant={items[sel] ? `pi-${sel}` : undefined} />
        <div class="palette-list" id="palette-list" role="listbox" ref={list}>
          {items.length === 0 ? <div class="palette-group">No matches</div> : null}
          {items.map((it, i) => {
            const head = it.group !== lastGroup ? <div class="palette-group" key={`g-${it.group}-${i}`}>{it.group}</div> : null;
            lastGroup = it.group;
            return (
              <>
                {head}
                <div key={it.id} id={`pi-${i}`} class="palette-item" role="option" aria-selected={i === sel}
                  onMouseMove={() => sel !== i && setSel(i)} onClick={() => runAt(i)}>
                  <span class="ellipsis">{it.title}</span>
                  {it.hint ? <span class="hint">{it.hint}</span> : null}
                  {it.chord ? <kbd>{formatChord(it.chord, platform.value)}</kbd> : null}
                </div>
              </>
            );
          })}
        </div>
        <div class="palette-foot"><span><kbd>↑↓</kbd> move</span><span><kbd>↵</kbd> run</span><span><kbd>&gt;</kbd> commands</span><span><kbd>#</kbd> tickets</span><span><kbd>@</kbd> sessions</span></div>
      </div>
    </>
  );
}
