// Queue view (W5, 3.4): toolbar, runner card, ticket list (keyboard + drag reorder), embedded ticket detail.
import { Component } from 'preact';
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { signal } from '@preact/signals';
import type { PanelCtx } from '../../shell/dock';
import { openPanel, openWeb } from '../../shell/dock';
import { useTopic, act, offlineReason, settings, store } from '../../core/app';
import { bridge, api, enc } from '../../core/bridge';
import type { Queue, TicketSummary, Gate } from '../../../shared/api';
import { Button, GateButton, Pill, EmptyState, TopicBody, AsOf } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { ago } from '../../core/format';
import { openDialog, closeDialog } from '../../shell/ui';
import { hooks } from '../../shell/commands';
import { isTyping } from '../../core/keymap';
import { VirtualList } from './virtual';
import { rowPill, runnerCard, counts, filterTickets, orderTickets, reconcile, moveLocal, neighbourBefore, keepSelection, slotStage, startModes, type PendingMove } from './model';

type Mode = 'container' | 'host' | 'single';
const lastMode = signal<Mode | null>(null);

function confirmDialog(title: string, body: string, confirm: string, danger = false): Promise<boolean> {
  return new Promise((resolve) => {
    const done = (v: boolean) => { closeDialog(); resolve(v); };
    openDialog(
      <div class="dialog glass glass--overlay" role="alertdialog" aria-label={title}>
        <header>{title}</header>
        <div class="dialog-body"><p class="muted">{body}</p></div>
        <footer><Button variant="ghost" onClick={() => done(false)}>Cancel</Button><Button variant={danger ? 'danger' : 'primary'} onClick={() => done(true)} autoFocus>{confirm}</Button></footer>
      </div>,
    );
  });
}

function HostConsent({ onDone }: { onDone: (ok: boolean) => void }) {
  const [ok, setOk] = useState(false);
  return (
    <div class="dialog glass glass--overlay" role="dialog" aria-label="Start on host">
      <header><Icon name="alert" />Start on the host</header>
      <div class="dialog-body">
        <p class="muted">Host mode runs the agents directly on this machine, outside the container and its firewall.</p>
        <label class="check"><input type="checkbox" checked={ok} onChange={(e) => setOk((e.target as HTMLInputElement).checked)} />I understand the agent runs with my user's access</label>
      </div>
      <footer><Button variant="ghost" onClick={() => onDone(false)}>Cancel</Button><Button variant="danger" disabled={!ok} onClick={() => onDone(true)}>Start on host</Button></footer>
    </div>
  );
}

export async function startQueue(mode: Mode, gate: Gate | undefined): Promise<void> {
  if (gate && !gate.ok) return;
  let confirm_host = false;
  if (mode === 'host') {
    confirm_host = await new Promise<boolean>((res) => openDialog(<HostConsent onDone={(v) => { closeDialog(); res(v); }} />));
    if (!confirm_host) return;
  }
  lastMode.value = mode;
  await act('/queue/start', confirm_host ? { mode, confirm_host: true } : { mode }, { refresh: ['queue'] });
}

function RunnerLog() {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    void bridge.api.get(api('/queue/runner-log')).then((r) => setText(r.ok && 'data' in r ? String((r.data as { text?: string })?.text ?? '') : r.ok ? '' : r.error.message));
  }, []);
  return (
    <div class="dialog dialog--wide glass glass--overlay" role="dialog" aria-label="Runner log">
      <header><Icon name="terminal" />Runner log<span class="spacer" /><Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Close" onClick={closeDialog} /></header>
      <div class="dialog-body"><pre class="pre small" style={{ maxHeight: '60vh', overflow: 'auto', margin: 0 }}>{text ?? 'Loading…'}</pre></div>
    </div>
  );
}

function StartSplit({ gate }: { gate: Gate | undefined }) {
  const [menu, setMenu] = useState(false);
  const def = (lastMode.value ?? settings.value?.queue?.startMode ?? 'container') as Mode;
  const label = startModes.find((m) => m.value === def)?.label ?? 'Start';
  const off = offlineReason.value;
  const reason = off || (gate && !gate.ok ? gate.reason : '');
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(false);
    window.addEventListener('pointerdown', close);
    return () => window.removeEventListener('pointerdown', close);
  }, [menu]);
  return (
    <div class="btn-group" style={{ position: 'relative' }}>
      <GateButton gate={gate} variant="primary" size="sm" icon="play" testid="queue-start" onClick={() => startQueue(def, gate)}>{label}</GateButton>
      <button type="button" class="btn btn--primary btn--sm btn--icon" aria-label="Start options" disabled={!!reason} title={reason}
        onPointerDown={(e) => e.stopPropagation()} onClick={() => setMenu(!menu)}><Icon name="chevronDown" size={14} /></button>
      {menu ? (
        <div class="menu glass glass--overlay" style={{ position: 'absolute', top: '30px', left: 0 }} onPointerDown={(e) => e.stopPropagation()} role="menu">
          {startModes.map((m) => <button key={m.value} role="menuitem" onClick={() => { setMenu(false); void startQueue(m.value, gate); }}>{m.label}</button>)}
        </div>
      ) : null}
    </div>
  );
}

const ROW_H = 32;   // .qrow height in queue.css
interface RowActions { select(k: string): void; open(k: string): void; move(k: string, before: string): void }
type RowProps = { t: TicketSummary; runner: Queue['runner']; selected: boolean; acts: { current: RowActions } };
const same = (a: unknown, b: unknown) => a === b || JSON.stringify(a) === JSON.stringify(b);
/** Memoised: a poll that changes one ticket re-renders that row only (IPC hands every ticket a new identity). */
class Row extends Component<RowProps> {
  shouldComponentUpdate(n: RowProps): boolean {
    const p = this.props;
    return n.selected !== p.selected || !same(n.t, p.t) || !same(n.runner, p.runner);
  }
  render() { return <RowView {...this.props} />; }
}
function RowView({ t, runner, selected, acts }: RowProps) {
  const p = rowPill(t, runner);
  const stage = p.live ? slotStage(runner, t.key) || t.stage : t.stage;
  const movable = t.actions?.move?.ok;
  return (
    <div class="qrow list-row" role="option" id={`qrow-${t.key}`} aria-selected={selected} data-testid={`ticket-row-${t.key}`} data-live={p.live ? '1' : undefined}
      onClick={() => acts.current.select(t.key)} onDblClick={() => acts.current.open(t.key)} draggable={!!movable}
      onDragStart={(ev) => { ev.dataTransfer?.setData('text/x-ticket', t.key); }}
      onDragOver={(e) => { if (movable) { e.preventDefault(); (e.currentTarget as HTMLElement).dataset.over = '1'; } }}
      onDragLeave={(e) => { delete (e.currentTarget as HTMLElement).dataset.over; }}
      onDrop={(e) => { delete (e.currentTarget as HTMLElement).dataset.over; const k = e.dataTransfer?.getData('text/x-ticket'); if (k && k !== t.key) acts.current.move(k, t.key); }}>
      <span class="qrow-grip" aria-hidden="true">{movable ? <Icon name="grip" size={14} /> : null}</span>
      <span class="qrow-pill" title={p.hint || undefined}><Pill tone={p.tone} dot={p.dot ?? undefined}>{p.label}</Pill></span>
      <span class="qrow-key mono">{t.key}</span>
      <span class="qrow-title ellipsis">{t.title || <span class="faint">untitled</span>}</span>
      <span class="qrow-stage ellipsis meta">{[stage, t.step].filter(Boolean).join(' · ')}</span>
      <span class="qrow-attempt meta tnum">{t.attempt > 1 ? `#${t.attempt}` : ''}</span>
      <span class="qrow-updated meta tnum">{ago(t.updated)}</span>
    </div>
  );
}

let TicketPanelMod: typeof import('../ticket/TicketPanel') | null = null;

function EmbeddedTicket({ ticketKey, ctx }: { ticketKey: string; ctx: PanelCtx }) {
  const [mod, setMod] = useState(TicketPanelMod);
  useEffect(() => { if (!mod) void import('../ticket/TicketPanel').then((m) => { TicketPanelMod = m; setMod(m); }); }, []);
  const sub = useMemo<PanelCtx>(() => ({
    ...ctx, id: `${ctx.id}/ticket`, kind: 'ticket', key: ticketKey,
    params: signal({ key: ticketKey, embedded: true }), setTitle: () => {}, setTabState: () => {}, update: () => {}, close: () => {},
    beforeClose: () => {}, onShow: ctx.onShow, onHide: ctx.onHide,
  }), [ticketKey]);
  if (!mod) return null;
  const C = mod.default;
  return <C key={ticketKey} ctx={sub} />;
}

export default function QueueView({ ctx }: { ctx: PanelCtx }) {
  const e = useTopic('queue');
  const [filter, setFilter] = useState('');
  const [archived, setArchived] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const selIndex = useRef(0);
  const [pending, setPending] = useState<PendingMove | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);
  const q = e?.data.value as Queue | undefined;
  useEffect(() => { ctx.setTitle('Queue'); }, []);
  const c = q ? counts(q.tickets) : null;
  useEffect(() => { ctx.setTabState(c && c.attention ? 'attention' : q?.runner?.running ? 'running' : 'idle'); }, [c?.attention, q?.runner?.running]);

  const serverOrder = q ? q.tickets.map((t) => t.key) : [];
  const rec = reconcile(serverOrder, pending, Date.now());
  useEffect(() => { if (pending && rec.pending === null) setPending(null); }, [rec.pending, pending]);
  useEffect(() => { if (!pending) return; const t = setTimeout(() => setPending((p) => (p === pending ? null : p)), 5200); return () => clearTimeout(t); }, [pending]);
  const source = q ? (archived ? q.archived : orderTickets(q.tickets, rec.order)) : [];
  const list = filterTickets(source, filter);
  const selected = keepSelection(sel, selIndex.current, list);
  useEffect(() => { if (selected !== sel) setSel(selected); const i = list.findIndex((t) => t.key === selected); if (i >= 0) selIndex.current = i; });

  const move = async (key: string, before: string | null) => {
    if (!q) return;
    const t = q.tickets.find((x) => x.key === key);
    if (!t?.actions?.move?.ok) return;
    const order = moveLocal(rec.order, key, before);
    setPending({ key, order, at: Date.now() });
    const r = await act(`/tickets/${enc(key)}/move`, { before: before ?? '' }, { refresh: ['queue'], quiet: true });
    if (!r.ok) setPending(null);
  };
  const select = (k: string | null) => { setSel(k); const i = list.findIndex((t) => t.key === k); if (i >= 0) selIndex.current = i; };
  const openTicket = (k: string) => openPanel('ticket', k, { title: k, params: { key: k } });
  const acts = useRef<RowActions>({ select, open: openTicket, move: (k, b) => void move(k, b) });
  acts.current = { select, open: openTicket, move: (k, b) => void move(k, b) };

  const onKey = (ev: KeyboardEvent) => {
    if (isTyping(ev.target) || ev.ctrlKey || ev.metaKey) return;
    const i = list.findIndex((t) => t.key === selected);
    const k = ev.key;
    if (ev.altKey && (k === 'ArrowUp' || k === 'ArrowDown')) {
      if (!selected) return;
      const b = neighbourBefore(rec.order, selected, k === 'ArrowUp' ? 'up' : 'down');
      if (b !== undefined) void move(selected, b);
      ev.preventDefault(); return;
    }
    if (ev.altKey) return;
    if (k === 'j' || k === 'ArrowDown') { select(list[Math.min(list.length - 1, i + 1)]?.key ?? null); ev.preventDefault(); }
    else if (k === 'k' || k === 'ArrowUp') { select(list[Math.max(0, i - 1)]?.key ?? null); ev.preventDefault(); }
    else if (k === 'Enter' && selected) { openTicket(selected); ev.preventDefault(); }
    else if (k === 'a') { hooks.addTickets(); ev.preventDefault(); }
    else if (k === 's' && q) { void startQueue((lastMode.value ?? settings.value?.queue?.startMode ?? 'container') as Mode, q.start); ev.preventDefault(); }
    else if (k === 'h' && q?.runner?.running && !q.runner.halting) { void act('/queue/halt', {}, { refresh: ['queue'] }); ev.preventDefault(); }
    else if (k === '/') { filterRef.current?.focus(); ev.preventDefault(); }
    else if ((k === 'n' || k === 'r' || k === '.') && selected) {
      root.current?.querySelector<HTMLElement>(k === 'n' ? '[data-testid="composer-input"]' : k === 'r' ? '[data-testid="ticket-action-retry"]' : '[data-testid^="ticket-action-"]')?.focus();
      ev.preventDefault();
    }
  };

  const card = runnerCard(q?.runner);
  const r = q?.runner;
  return (
    <div class="view queue-view" data-testid="view-queue" ref={root} onKeyDown={onKey}>
      <div class="view-head toolbar">
        <Button size="sm" icon="plus" testid="queue-add" onClick={() => hooks.addTickets()} disabled={!!offlineReason.value} title={offlineReason.value || 'Add tickets (A)'}>Add tickets</Button>
        <StartSplit gate={q?.start} />
        <Button size="sm" icon="pause" testid="queue-halt" disabled={!r?.running || r.halting || !!offlineReason.value}
          title={!r?.running ? 'the queue is not running' : r.halting ? 'already halting' : 'Halt after this stage (H)'}
          onClick={() => act('/queue/halt', {}, { refresh: ['queue'] })}>Halt</Button>
        <Button size="sm" variant="danger" icon="stop" testid="queue-end" disabled={!r?.running || !!offlineReason.value}
          title={r?.running ? 'End now: kills the running stage' : 'the queue is not running'}
          onClick={async () => {
            if (await confirmDialog('End the run now?', `This kills ${r?.ticket || 'the running ticket'}${r?.stage ? ` in ${r.stage}` : ''} immediately.`, 'End now', true))
              await act('/queue/end', {}, { refresh: ['queue'] });
          }}>End</Button>
        <span class="spacer" />
        <div class="search-field"><Icon name="search" size={14} />
          <input ref={filterRef} class="input input--bare" placeholder="Filter tickets" value={filter} aria-label="Filter tickets"
            onInput={(ev) => setFilter((ev.target as HTMLInputElement).value)} onKeyDown={(ev) => { if (ev.key === 'Escape') { setFilter(''); (ev.target as HTMLElement).blur(); } }} />
        </div>
        <button class="btn btn--sm btn--ghost" aria-pressed={archived} onClick={() => setArchived(!archived)} title="Show archived tickets">
          <Icon name="archive" size={14} />Archived{q?.archived?.length ? <span class="meta tnum">{q.archived.length}</span> : null}</button>
        <Button size="sm" variant="ghost" iconOnly icon="terminal" aria-label="Runner log" title="Runner log" onClick={() => openDialog(<RunnerLog />)} />
        <Button size="sm" variant="ghost" iconOnly icon="globe" aria-label="Open web version" title="Open web version" onClick={() => openWeb('/runs/queue', { title: 'Queue (web)' })} />
      </div>
      <TopicBody e={e} webPath="/runs/queue" openWeb={(p) => openWeb(p)}>
        {(qd: Queue) => (
          <div class="queue-split">
            <div class="queue-left">
              <div class={`runner-card card runner-card--${card.phase}`} data-phase={card.phase}>
                <span class={`dot ${card.phase === 'running' ? 'dot--live' : card.phase === 'halting' || card.phase === 'starting' ? 'dot--warn' : ''}`} />
                <div class="col grow" style={{ gap: '2px', minWidth: 0 }}>
                  <div class="row"><strong>{card.title}</strong>{card.mode && card.phase !== 'idle' ? <Pill tone="info">{card.mode}</Pill> : null}
                    {card.slots > 1 ? <span class="meta tnum">{card.busy}/{card.slots} slots</span> : null}</div>
                  <div class="meta ellipsis">{card.detail || (card.phase === 'idle' ? (qd.start.ok ? 'Ready to start' : qd.start.reason) : '')}{r?.model ? ` · ${r.model}` : ''}{r?.since && card.phase === 'running' ? ` · ${ago(r.since)}` : ''}</div>
                </div>
                {c ? <div class="runner-counts tnum">
                  {c.attention ? <Pill tone="accent">{c.attention} waiting</Pill> : null}
                  <span class="meta">{c.queued} queued · {c.running} running · {c.done} done{c.blocked ? ` · ${c.blocked} blocked` : ''}</span>
                </div> : null}
                <AsOf e={e} />
              </div>
              {list.length === 0 ? (
                filter ? <EmptyState icon="search" text={`No tickets match “${filter}”.`} action={<Button size="sm" onClick={() => setFilter('')}>Clear filter</Button>} />
                  : archived ? <EmptyState icon="archive" text="No archived tickets." />
                  : <EmptyState icon="queue" text="No tickets queued." action={<Button variant="primary" icon="plus" onClick={() => hooks.addTickets()}>Add tickets</Button>} />
              ) : (
                <VirtualList class="qlist" role="listbox" aria-label="Tickets" tabIndex={0} aria-activedescendant={selected ? `qrow-${selected}` : undefined}
                  items={list} rowHeight={ROW_H} focusIndex={selected ? list.findIndex((t) => t.key === selected) : -1}
                  row={(t: TicketSummary) => <Row key={t.key} t={t} runner={qd.runner} selected={t.key === selected} acts={acts} />} />
              )}
            </div>
            <div class="queue-right">
              {selected ? <EmbeddedTicket ticketKey={selected} ctx={ctx} /> : <EmptyState icon="queue" text="Select a ticket to see its details." />}
            </div>
          </div>
        )}
      </TopicBody>
    </div>
  );
}
void store;
