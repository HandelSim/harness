// Local helpers for the ticket panel: persisted drafts, confirm/prompt dialogs, a small anchored menu.
import { useEffect, useRef, useState } from 'preact/hooks';
import type { ComponentChildren } from 'preact';
import { appState, patchState } from '../../core/app';
import { dialog, openDialog, closeDialog } from '../../shell/ui';
import { Button } from '../../ui/kit';

/** Draft text persisted in AppState.drafts (W5: restored after restart, cleared on success). Never touched by live data. */
export function useDraft(k: string): [string, (v: string) => void, () => void] {
  const [v, setV] = useState<string>(() => appState.peek()?.drafts?.[k] ?? '');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef<{ k: string; text: string } | null>(null);
  const cur = useRef(k);
  // Write the pending draft now (on key change and unmount a pending write is flushed, never dropped).
  const flush = () => {
    if (timer.current) { clearTimeout(timer.current); timer.current = null; }
    const p = pending.current;
    pending.current = null;
    if (!p) return;
    const d = { ...(appState.peek()?.drafts ?? {}) };
    if (p.text) d[p.k] = p.text; else delete d[p.k];
    patchState({ drafts: d });
  };
  useEffect(() => { if (cur.current !== k) { flush(); cur.current = k; setV(appState.peek()?.drafts?.[k] ?? ''); } }, [k]);
  useEffect(() => flush, []);
  const write = (text: string) => {
    if (pending.current && pending.current.k !== k) flush();
    pending.current = { k, text };
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(flush, 400);
  };
  const set = (text: string) => { setV(text); write(text); };
  const clear = () => { setV(''); write(''); };
  return [v, set, clear];
}

/** Persisted JSON draft (answers form). */
export function useJsonDraft<T>(k: string, init: T): [T, (v: T) => void, () => void] {
  const [raw, set, clear] = useDraft(k);
  let val = init;
  if (raw) { try { val = { ...init, ...JSON.parse(raw) }; } catch { /* corrupt draft: start fresh */ } }
  return [val, (v: T) => set(JSON.stringify(v)), clear];
}

export function confirmDialog(o: { title: string; body: ComponentChildren; ok: string; danger?: boolean }): Promise<boolean> {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v: boolean) => { if (done) return; done = true; if (dialog.peek() === node) closeDialog(); resolve(v); };
    const node = (
      <div class="dialog glass glass--overlay" role="alertdialog" aria-label={o.title}
        ref={(el) => { if (el) setTimeout(() => el.querySelector<HTMLButtonElement>('[data-autofocus]')?.focus(), 0); }}>
        <header>{o.title}</header>
        <div class="dialog-body">{o.body}</div>
        <footer>
          <Button variant="ghost" onClick={() => finish(false)}>Cancel</Button>
          <Button variant={o.danger ? 'danger' : 'primary'} data-autofocus="1" onClick={() => finish(true)}>{o.ok}</Button>
        </footer>
      </div>
    );
    openDialog(node);
    // Escape / scrim / another dialog replace it without finish(): resolve false once it is no longer shown.
    const check = setInterval(() => { if (dialog.peek() !== node) { clearInterval(check); if (!done) { done = true; resolve(false); } } }, 250);
  });
}

export function promptDialog(o: { title: string; label: string; ok: string; required?: boolean; placeholder?: string; extra?: (set: (k: string, v: boolean) => void, vals: Record<string, boolean>) => ComponentChildren }):
  Promise<{ text: string; flags: Record<string, boolean> } | null> {
  return new Promise((resolve) => {
    let done = false;
    let text = '';
    const flags: Record<string, boolean> = {};
    const finish = (v: { text: string; flags: Record<string, boolean> } | null) => { if (done) return; done = true; if (dialog.peek() === node) closeDialog(); resolve(v); };
    function Body() {
      const [t, setT] = useState('');
      const [f, setF] = useState<Record<string, boolean>>({});
      text = t;
      Object.assign(flags, f);
      return (
        <div class="dialog glass glass--overlay" role="dialog" aria-label={o.title}>
          <header>{o.title}</header>
          <div class="dialog-body">
            <label class="field"><span class="label">{o.label}</span>
              <textarea class="textarea" rows={4} value={t} placeholder={o.placeholder} autoFocus
                onInput={(e) => setT((e.target as HTMLTextAreaElement).value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (!o.required || t.trim()) finish({ text: t, flags: f }); } }} />
            </label>
            {o.extra?.((k, v) => setF((p) => ({ ...p, [k]: v })), f)}
          </div>
          <footer>
            <Button variant="ghost" onClick={() => finish(null)}>Cancel</Button>
            <Button variant="primary" disabled={!!o.required && !t.trim()} onClick={() => finish({ text, flags })}>{o.ok}</Button>
          </footer>
        </div>
      );
    }
    const node = <Body />;
    openDialog(node);
    const check = setInterval(() => { if (dialog.peek() !== node) { clearInterval(check); if (!done) { done = true; resolve(null); } } }, 250);
  });
}

/** Menu anchored under a button (fixed position, closes on outside click / Escape). */
export function AnchoredMenu({ anchor, onClose, children }: { anchor: HTMLElement; onClose: () => void; children: ComponentChildren }) {
  const el = useRef<HTMLDivElement>(null);
  const r = anchor.getBoundingClientRect();
  useEffect(() => {
    const off = (e: PointerEvent) => { if (el.current && !el.current.contains(e.target as Node) && !anchor.contains(e.target as Node)) onClose(); };
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); onClose(); anchor.focus(); } };
    window.addEventListener('pointerdown', off, true);
    window.addEventListener('keydown', esc, true);
    setTimeout(() => el.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus(), 0);
    return () => { window.removeEventListener('pointerdown', off, true); window.removeEventListener('keydown', esc, true); };
  }, []);
  const left = Math.min(r.left, window.innerWidth - 240);
  return <div ref={el} class="menu glass glass--overlay" role="menu" style={{ top: `${r.bottom + 4}px`, left: `${Math.max(8, left)}px` }}>{children}</div>;
}
