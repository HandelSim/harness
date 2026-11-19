// Ticket detail panel (PLAN 3.5, W5): header (key, title, status, stage timeline, actions bar) and the
// Overview / Live / Plan / Notes / Sessions tabs. Every enable/disable comes from ticket.actions (server Gates);
// every server string renders as text. Native edit is not built: Edit opens the serve page in a Web panel.
import { useEffect, useState } from 'preact/hooks';
import type { ActionResult, Ticket, TicketAction, Topic } from '../../../shared/api';
import { act, store, useTopic } from '../../core/app';
import { bridge, enc } from '../../core/bridge';
import { ago, dateTime, plural } from '../../core/format';
import { openPanel, openWeb, type PanelCtx, type TabState } from '../../shell/dock';
import { AsOf, Button, EmptyState, GateButton, Pill, Tabs, TopicBody } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import {
  defaultTab, diffstatText, isStopping, rec, safeUrl, SOFT_ERRORS, statusLabel, statusTone, str, timeline,
  visibleBarActions, TICKET_TABS, type Step, type TicketTab,
} from './model';
import { AnchoredMenu, confirmDialog, promptDialog } from './parts';
import { CHANGED_MSG, Criteria, LiveTab, NotesTab, PlanTab, type ComposeMode, type Run } from './tabs';
import { ChangesTab } from './DiffView';

const TAB_STATE: Record<string, TabState> = {
  running: 'working', 'needs-answers': 'attention', 'plan-review': 'attention', interrupted: 'attention',
  blocked: 'error', done: 'ended',
};

export default function TicketPanel({ ctx }: { ctx: PanelCtx }) {
  const key = String(ctx.params.value.key ?? ctx.key ?? '');
  const topic = `ticket:${key}` as `ticket:${string}`;
  const e = useTopic(topic);
  const data = e?.data.value;
  const gone = !!e?.gone.value || (!!data && 'gone' in data);
  const t = data && !('gone' in data) ? data : null;
  const status = t?.state.status ?? '';
  const p = ctx.params.value.tab;
  const [tab, setTabRaw] = useState<TicketTab | null>(TICKET_TABS.includes(p as TicketTab) ? (p as TicketTab) : null);
  const cur: TicketTab = tab ?? (t ? defaultTab(status) : 'overview');
  const [mode, setMode] = useState<ComposeMode>('note');
  const [focusTick, setFocusTick] = useState(0);
  const [barErr, setBarErr] = useState('');
  const [planNotice, setPlanNotice] = useState('');
  const visible = ctx.visible.value;
  const webPath = `/runs/ticket/${enc(key)}`;
  const toWeb = (path: string) => { openWeb(path); };

  useEffect(() => { ctx.setTitle(key); }, [key]);
  // Pick the status's default tab once, when the data first arrives; later status changes never switch tabs.
  useEffect(() => { if (t && tab === null) setTabRaw(defaultTab(t.state.status)); }, [!!t]);
  useEffect(() => { ctx.setTabState(gone ? 'ended' : TAB_STATE[status] ?? 'idle'); }, [status, gone]);

  const setTab = (x: TicketTab) => { setTabRaw(x); ctx.update({ tab: x }); };
  const refresh: Topic[] = [topic, 'queue'];
  const run: Run = async (action, body = {}, inline = false) => {
    if (!inline) setBarErr('');
    const r = await act(`/tickets/${enc(key)}/${action}`, body, { refresh });
    if (!r.ok && !inline && !SOFT_ERRORS.has(r.error.code)) setBarErr(r.error.message || r.error.code);
    return r;
  };
  const approve = async (): Promise<ActionResult | undefined> => {
    if (!t) return undefined;
    setPlanNotice('');
    const r = await run('approve', { sig: t.state.plan_sig }, true);
    if (!r.ok) {
      if (r.error.code === 'changed') { setPlanNotice(r.error.message || CHANGED_MSG); setTab('plan'); void store.load(topic); }
      else if (!SOFT_ERRORS.has(r.error.code)) setBarErr(r.error.message || r.error.code);
    }
    return r;
  };
  const followup = () => { setMode('followup'); setTab('notes'); setFocusTick((n) => n + 1); };

  if (gone) {
    return (
      <div class="view" data-testid="ticket-gone">
        <EmptyState icon="trash" text={`${key} was removed. This ticket is no longer in the queue.`}
          action={<Button onClick={() => ctx.close()} testid="ticket-gone-close">Close</Button>} />
      </div>
    );
  }
  if (!t) {
    return (
      <div class="view tk" data-testid="ticket-panel">
        <div class="view-head"><h1 class="mono">{key}</h1><span class="spacer" /><AsOf e={e} /></div>
        <div class="view-body"><TopicBody e={e} webPath={webPath} openWeb={toWeb}>{() => null}</TopicBody></div>
      </div>
    );
  }

  const s = t.state;
  const stopping = isStopping(s);
  const tabs = [
    { id: 'overview' as const, label: 'Overview' },
    { id: 'live' as const, label: 'Live', badge: status === 'running' ? <span class="dot dot--live" /> : undefined },
    { id: 'plan' as const, label: status === 'needs-answers' ? 'Answers' : 'Plan', badge: s.questions?.length && (status === 'needs-answers' || status === 'plan-review') ? String(s.questions.length) : undefined },
    { id: 'notes' as const, label: 'Notes', badge: s.notes?.length ? String(s.notes.length) : undefined },
    { id: 'sessions' as const, label: 'Sessions', badge: t.sessions.length ? String(t.sessions.length) : undefined },
    { id: 'changes' as const, label: 'Changes' },
  ];

  return (
    <div class="view tk" data-testid="ticket-panel" data-status={status}>
      <header class="tk-head">
        <div class="tk-head-top">
          <span class="tk-key mono" data-testid="ticket-key">{t.key}</span>
          <Pill tone={statusTone(status)} dot={status === 'running' ? 'live' : undefined} testid="ticket-status">{statusLabel(status)}</Pill>
          {stopping ? <Pill tone="warn" dot="warn" testid="ticket-stopping">{s.stuck ? 'stopping (stuck)' : 'stopping…'}</Pill> : null}
          {t.archived ? <Pill>archived</Pill> : null}
          <span class="spacer" />
          <AsOf e={e} />
          <Button size="sm" variant="ghost" icon="globe" iconOnly aria-label="Open web version" title="Open web version"
            testid="ticket-open-web" onClick={() => toWeb(webPath)} />
        </div>
        <h1 class="tk-title" title={s.title}>{s.title || <span class="faint">Untitled</span>}</h1>
        <Timeline steps={timeline(s)} />
        <ActionsBar t={t} run={run} approve={approve} onFollowup={followup} stopping={stopping} toWeb={toWeb} />
        {barErr ? (
          <div class="inline-err tk-bar-err" role="alert" data-testid="ticket-action-error">
            <span class="grow">{barErr}</span>
            <Button size="sm" variant="ghost" icon="x" iconOnly aria-label="Dismiss" onClick={() => setBarErr('')} />
          </div>
        ) : null}
      </header>
      <Tabs tabs={tabs} value={cur} onChange={setTab} testidPrefix="ticket-tab-" />
      <div class={`view-body tk-body tk-body--${cur}`} role="tabpanel">
        {cur === 'overview' ? <Overview t={t} /> : null}
        {cur === 'live' ? <LiveTab t={t} visible={visible} /> : null}
        {cur === 'plan' ? <PlanTab t={t} run={run} approve={approve} notice={planNotice} setNotice={setPlanNotice} /> : null}
        {cur === 'notes' ? <NotesTab t={t} run={run} mode={mode} setMode={setMode} focusTick={focusTick} /> : null}
        {cur === 'sessions' ? <Sessions t={t} /> : null}
        {cur === 'changes' ? <ChangesTab ticketKey={t.key} t={t} /> : null}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ header parts
function Timeline({ steps }: { steps: Step[] }) {
  if (!steps.length) return null;
  return (
    <ol class="tk-timeline" aria-label="Stages" data-testid="ticket-timeline">
      {steps.map((st) => (
        <li key={st.stage} class="tk-step" data-state={st.state} title={`${st.stage}: ${st.state}${st.attempts > 1 ? ` (${st.attempts} attempts)` : ''}`}>
          <span class="tk-step-dot">{st.state === 'done' ? <Icon name="check" size={10} /> : st.state === 'failed' ? <Icon name="x" size={10} /> : null}</span>
          <span class="tk-step-name">{st.stage}</span>
          {st.attempts > 1 ? <span class="tk-step-n tnum">×{st.attempts}</span> : null}
        </li>
      ))}
    </ol>
  );
}

function ActionsBar({ t, run, approve, onFollowup, stopping, toWeb }: {
  t: Ticket; run: Run; approve: () => Promise<unknown>; onFollowup: () => void; stopping: boolean; toWeb: (p: string) => void;
}) {
  const [menu, setMenu] = useState<HTMLElement | null>(null);
  const status = t.state.status;
  const shown = new Set(visibleBarActions(t));
  const choices = t.retry_choices ?? [];
  const retry = (stage: string) => run('retry', stage ? { stage } : {});
  const b = (a: TicketAction) => {
    if (!shown.has(a)) return null;
    const c = { key: a, gate: t.actions[a], size: 'sm' as const, testid: `ticket-action-${a}` };
    switch (a) {
      case 'approve': return <GateButton {...c} variant="primary" icon="check" onClick={approve}>Approve plan</GateButton>;
      case 'continue': {
        const resume = status === 'interrupted';
        return (
          <GateButton {...c} variant={resume ? 'primary' : 'default'} icon="play" onClick={async () => {
            const r = await promptDialog({ title: `${resume ? 'Resume' : 'Continue'} ${t.key}`, label: 'Message for the agent (optional)', ok: resume ? 'Resume' : 'Continue', placeholder: 'Ctrl+Enter to send' });
            if (r) await run('continue', r.text.trim() ? { text: r.text.trim() } : {});
          }}>{resume ? 'Resume' : 'Continue'}</GateButton>
        );
      }
      case 'retry':
        return (
          <GateButton {...c} icon="retry" aria-haspopup={choices.length > 1 ? 'menu' : undefined}
            onClick={(ev) => (choices.length > 1 ? setMenu(ev.currentTarget as HTMLElement) : retry(choices[0]?.value ?? ''))}>
            Retry{choices.length > 1 ? <Icon name="chevronDown" size={12} /> : null}
          </GateButton>
        );
      case 'followup': return <GateButton {...c} icon="chat" onClick={onFollowup}>Follow-up</GateButton>;
      case 'stop':
        return (
          <GateButton {...c} variant="danger" icon="stop" onClick={async () => {
            const ok = await confirmDialog({ title: `Stop ${t.key}?`, body: 'The running stage is stopped. You can continue or retry the ticket later.', ok: 'Stop', danger: true });
            if (ok) await run('stop', {});
          }}>{stopping ? 'Stopping…' : 'Stop'}</GateButton>
        );
      case 'edit': return <GateButton {...c} variant="ghost" icon="edit" title="Opens the web editor" onClick={() => toWeb(`/runs/ticket/${enc(t.key)}/edit`)}>Edit</GateButton>;
      case 'archive': return <GateButton {...c} variant="ghost" icon="archive" onClick={() => run('archive', {})}>Archive</GateButton>;
      case 'unarchive': return <GateButton {...c} variant="ghost" icon="archive" onClick={() => run('unarchive', {})}>Unarchive</GateButton>;
      case 'remove':
        return (
          <GateButton {...c} variant="ghost" icon="trash" class="tk-remove" onClick={async () => {
            const ok = await confirmDialog({ title: `Remove ${t.key}?`, body: <>The ticket is removed from the queue. <b>This cannot be undone.</b></>, ok: 'Remove', danger: true });
            if (ok) await run('remove', {});
          }}>Remove</GateButton>
        );
      default: return null;
    }
  };
  return (
    <div class="tk-actions" role="toolbar" aria-label="Ticket actions">
      {b('approve')}{b('continue')}{b('retry')}{b('followup')}{b('stop')}
      <span class="spacer" />
      {b('edit')}{b('archive')}{b('unarchive')}{b('remove')}
      {menu ? (
        <AnchoredMenu anchor={menu} onClose={() => setMenu(null)}>
          {choices.map((ch) => (
            <button key={ch.value || 'here'} type="button" role="menuitem" data-testid={`ticket-retry-${ch.value || 'here'}`}
              onClick={() => { setMenu(null); void retry(ch.value); }}>
              {ch.value ? `From ${ch.label}` : ch.label.charAt(0).toUpperCase() + ch.label.slice(1)}
            </button>
          ))}
        </AnchoredMenu>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Overview
function Overview({ t }: { t: Ticket }) {
  const s = t.state;
  const why = str((t as unknown as Record<string, unknown>).why);
  const packet = rec(s.packet);
  const push = rec(s.push);
  const pushes = Array.isArray(s.pushes) ? (s.pushes as unknown[]).map(rec).filter(Boolean) as Record<string, unknown>[] : [];
  const left = Array.isArray(s.left_uncommitted) ? (s.left_uncommitted as unknown[]).map(str).filter(Boolean) : [];
  const commits = Array.isArray(s.commits) ? s.commits.length : typeof s.commits === 'number' ? s.commits : 0;
  const stat = diffstatText(s.diffstat);
  const compare = safeUrl(s.compare_url);
  const history = [...(s.history ?? [])].reverse();
  const details: [string, string][] = ([
    ['Stage', s.stage], ['Attempt', s.attempt ? String(s.attempt) : ''], ['Project', str(s.project)], ['Branch', str(s.branch)],
    ['Base', str(s.base)], ['Created', s.created ? dateTime(s.created) : ''], ['Updated', s.updated ? `${ago(s.updated)} · ${dateTime(s.updated)}` : ''],
  ] as [string, string][]).filter(([, v]) => !!v);
  const blocked = s.status === 'blocked' || s.status === 'stopped';
  return (
    <div class="tk-overview" data-testid="ticket-overview">
      {s.reason || why ? (
        <div class={`notice ${blocked ? 'notice--warn' : ''}`} data-testid="ticket-reason">
          <Icon name={blocked ? 'alert' : 'info'} size={16} />
          <div class="grow">{s.reason ? <div class="tk-pre">{s.reason}</div> : null}{why && why !== s.reason ? <div class="meta tk-pre">{why}</div> : null}</div>
        </div>
      ) : null}
      {packet && str(packet.question) ? (
        <section class="tk-card tk-card--accent">
          <div class="section-title">Decision needed</div>
          <div class="tk-pre">{str(packet.question)}</div>
          {str(packet.default) ? <div class="meta">Default: <span class="tk-pre">{str(packet.default)}</span></div> : null}
          {str(packet.reason) ? <div class="meta tk-pre">{str(packet.reason)}</div> : null}
        </section>
      ) : null}
      {stat || commits || compare || push || left.length ? (
        <section class="tk-card" data-testid="ticket-result">
          <div class="section-title">Result</div>
          <div class="tk-result">
            {stat ? <span class="tk-stat mono tnum">{stat}</span> : null}
            {commits ? <span class="meta">{plural(commits, 'commit')}</span> : null}
            {compare ? <Button size="sm" variant="ghost" icon="external" onClick={() => bridge.shell.openExternal(compare)}>Compare</Button> : null}
          </div>
          {push || pushes.length ? (
            <dl class="kv tk-kv">
              {[...(push ? [push] : []), ...pushes].flatMap((pp, i) => Object.entries(pp).filter(([, v]) => str(v)).map(([k, v]) => [
                <dt key={`k${i}${k}`}>{k}</dt>, <dd key={`v${i}${k}`} class="mono ellipsis" title={str(v)}>{str(v)}</dd>,
              ]))}
            </dl>
          ) : null}
          {left.length ? <div class="notice notice--warn"><Icon name="alert" size={16} /><div>Left uncommitted in {left.join(', ')}</div></div> : null}
        </section>
      ) : null}
      <div class="tk-grid">
        <section class="tk-card">
          <div class="section-title">Details</div>
          <dl class="kv tk-kv">{details.flatMap(([k, v]) => [<dt key={`k${k}`}>{k}</dt>, <dd key={`v${k}`} class="ellipsis" title={v}>{v}</dd>])}</dl>
        </section>
        {s.criteria?.length ? (
          <section class="tk-card">
            <div class="section-title">Acceptance criteria <span class="badge">{s.criteria.length}</span></div>
            <Criteria list={s.criteria} />
          </section>
        ) : null}
      </div>
      <section class="tk-card" data-testid="ticket-history">
        <div class="section-title">History</div>
        {history.length ? (
          <ol class="tk-history">
            {history.map((h, i) => (
              <li key={i} data-fail={/fail|error|block|refus|stop|abort/i.test(h.result ?? '') ? '1' : undefined}>
                <span class="meta tnum tk-h-at" title={dateTime(h.at)}>{ago(h.at)}</span>
                <span class="tk-h-stage">{h.stage}{h.attempt > 1 ? <span class="faint"> #{h.attempt}</span> : null}</span>
                <span class="tk-h-result">{h.result}</span>
                {h.details ? <div class="tk-h-details meta tk-pre">{h.details}</div> : null}
              </li>
            ))}
          </ol>
        ) : <p class="muted small">Nothing has run yet.</p>}
      </section>
      {t.log ? (
        <details class="tk-card tk-log">
          <summary class="section-title">Log</summary>
          <pre class="pre">{t.log}</pre>
        </details>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ Sessions
function Sessions({ t }: { t: Ticket }) {
  const list = [...t.sessions].sort((a, b) => b.created - a.created);
  if (!list.length) return <EmptyState icon="sessions" text="No agent sessions for this ticket yet." />;
  return (
    <div class="tk-sessions">
      {list.map((x) => (
        <button key={x.sid} type="button" class="list-row tk-session" data-testid={`ticket-session-${x.sid}`}
          onClick={() => openPanel('transcript', x.sid, { title: `${t.key} ${x.label || x.stage}`, params: { sid: x.sid } })}>
          <span class={`dot ${x.working ? 'dot--live' : 'tk-dot-idle'}`} aria-label={x.working ? 'working' : 'ended'} />
          <span class="grow ellipsis">{x.label || x.stage}</span>
          {x.kind === 'debug' ? <Pill tone="warn">debug</Pill> : null}
          <span class="meta tnum" title={dateTime(x.created)}>{x.created ? ago(x.created) : ''}</span>
          <Icon name="chevronRight" size={14} />
        </button>
      ))}
    </div>
  );
}
