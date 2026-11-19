// Home view (PLAN 3.3; parity with serve's `/`: vault chip, opencode web launch, proxy state, live runs, pages, usage,
// prompt modes, vault activity) plus a queue summary and quick actions. Topics: home, status, queue.
import { useEffect, useMemo, useState } from 'preact/hooks';
import type { Home, SessionRow, Status } from '../../../shared/api';
import { backend, keyState, useTopic } from '../../core/app';
import { bridge } from '../../core/bridge';
import { ago, basename, tokens } from '../../core/format';
import { openPanel, openView, openWeb, type PanelCtx } from '../../shell/dock';
import { hooks } from '../../shell/commands';
import { AsOf, Button, ErrorPanel, Pill, Skeleton } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { activityRows, filterPages, greeting, pubPath, queueSummary, usageView } from './model';
import css from './home.css' with { type: 'text' };

if (!document.getElementById('css-home')) { const el = document.createElement('style'); el.id = 'css-home'; el.textContent = css as string; document.head.append(el); }

const n = (v: number) => v.toLocaleString();
const openSession = (r: SessionRow) => openPanel('transcript', r.id, { title: r.title || r.id.slice(0, 12), params: { sid: r.id } });
const onEnter = (f: () => void) => (e: KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); f(); } };

function Card({ title, icon, children, testid, actions, wide }: { title: string; icon: string; children: preact.ComponentChildren; testid: string; actions?: preact.ComponentChildren; wide?: boolean }) {
  return (
    <section class={`card home-card${wide ? ' home-card--wide' : ''}`} data-testid={testid} aria-label={title}>
      <header class="home-card-head"><Icon name={icon} size={14} /><span class="section-title">{title}</span><span class="grow" />{actions}</header>
      {children}
    </section>
  );
}

function Health({ st }: { st: Status | undefined }) {
  const b = backend.value;
  const key = keyState.value?.keys.find((k) => k.active);
  const probe = key?.probe?.state;
  const proxy = st?.proxy;
  return (
    <div class="home-health">
      <div class="card home-tile" data-testid="home-backend">
        <span class="meta">Backend</span>
        <span class="row"><span class={`dot ${b?.phase === 'up' ? 'dot--ok' : 'dot--err'}`} />{b?.phase === 'up' ? `serve :${b.port ?? ''}` : (b?.phase ?? 'locating')}</span>
        {st ? <span class="meta mono ellipsis" title={st.version.describe}>{st.version.branch} · {st.version.sha.slice(0, 7)}</span> : null}
      </div>
      <div class="card home-tile" data-testid="home-proxy">
        <span class="meta">Model proxy</span>
        {proxy ? <span class="row"><span class={`dot ${proxy.up ? 'dot--ok' : 'dot--err'}`} />{proxy.up ? 'up' : 'down'}<span class="meta ellipsis" title={proxy.url}>{proxy.detail}</span></span>
          : <span class="meta">not configured</span>}
        {proxy && !proxy.up ? <Button size="sm" icon="refresh" testid="home-restart" onClick={() => void bridge.backend.action({ action: 'restart' })}>Restart backend</Button> : null}
      </div>
      <div class="card home-tile" data-testid="home-key" role="button" tabIndex={0} onClick={() => openView('keys')} onKeyDown={onEnter(() => openView('keys'))}>
        <span class="meta">Key</span>
        <span class="row"><span class={`dot ${probe === 'ok' ? 'dot--ok' : probe === 'locked' || probe === 'rejected' ? 'dot--err' : ''}`} />{key ? key.label : 'no key in the app'}</span>
        <span class="meta">{probe ? `${probe}${key?.probe?.at ? ` · ${ago(key.probe.at)}` : ''}` : 'not checked'}</span>
      </div>
      <div class="card home-tile" data-testid="home-vault">
        <span class="meta">Vault</span>
        {st?.vault ? <span class="row"><Icon name="folder" size={13} /><span class="ellipsis" title={st.vault.dir}>{st.vault.name || basename(st.vault.dir)}</span></span>
          : <span class="meta">no vault selected (harness vault use &lt;name&gt;)</span>}
      </div>
    </div>
  );
}

function QueueCard() {
  const e = useTopic('queue');
  const s = queueSummary(e?.data.value);
  return (
    <Card title="Queue" icon="queue" testid="home-queue" actions={<Button size="sm" variant="ghost" onClick={() => openView('queue')}>Open</Button>}>
      {!s ? (e?.error.value ? <span class="meta">{e.error.value.message}</span> : <Skeleton rows={2} />) : (
        <div class="col home-gap">
          <span class="row"><span class={`dot ${s.runnerOn ? 'dot--live' : ''}`} /><span data-testid="home-runner">{s.runner}</span></span>
          <div class="home-counts tnum">
            {([['running', s.running], ['waiting on you', s.waiting], ['queued', s.queued], ['stuck', s.stuck], ['done', s.done]] as const).map(([k, v]) => (
              <span key={k} class="home-count"><b>{v}</b><span class="meta">{k}</span></span>
            ))}
          </div>
          {s.attention.map((t) => (
            <div key={t.key} class="list-row" role="button" tabIndex={0} data-testid={`home-attention-${t.key}`}
              onClick={() => openPanel('ticket', t.key, { title: t.key, params: { key: t.key } })} onKeyDown={onEnter(() => openPanel('ticket', t.key, { title: t.key, params: { key: t.key } }))}>
              <Pill tone="warn">{t.status === 'plan-review' ? 'plan review' : 'needs answers'}</Pill><span class="mono">{t.key}</span><span class="ellipsis meta">{t.title}</span>
            </div>
          ))}
          {!s.total ? <span class="meta">No tickets queued.</span> : null}
        </div>
      )}
    </Card>
  );
}

function Pages({ h }: { h: Home }) {
  const [q, setQ] = useState('');
  const { shown, more, total } = filterPages(h.pages as { rel: string; title: string; url?: string }[], q);
  return (
    <Card title="Vault pages" icon="file" testid="home-pages">
      {!total ? <span class="meta">No pages yet. HTML files the agent writes under the vault's pub/ folder show up here.</span> : (
        <div class="col home-gap">
          {total > 8 ? <input class="input" placeholder="Filter by title or path" aria-label="Filter pages" value={q} onInput={(ev) => setQ((ev.target as HTMLInputElement).value)} /> : null}
          {shown.map((p) => {
            const go = () => openWeb(pubPath(p.rel), { title: p.title || p.rel });
            return (
              <div key={p.rel} class="list-row" role="button" tabIndex={0} data-testid={`home-page-${p.rel}`} onClick={go} onKeyDown={onEnter(go)}>
                <span class="ellipsis grow">{p.title || p.rel}</span><span class="meta mono ellipsis">{p.rel}</span>
              </div>
            );
          })}
          {more ? <span class="meta">{more} more not shown; narrow them with the filter</span> : null}
          {q && !shown.length ? <span class="meta">No page matches.</span> : null}
        </div>
      )}
    </Card>
  );
}

function Body({ h, st }: { h: Home; st: Status | undefined }) {
  const usage = useMemo(() => usageView(h.usage), [h.usage]);
  const activity = useMemo(() => activityRows(h.activity), [h.activity]);
  const live = Array.isArray(h.live) ? h.live : [];
  const oc = st?.opencode;
  return (
    <div class="home-grid">
      <Card title="Quick actions" icon="sparkle" testid="home-actions">
        <div class="home-actions">
          <Button variant="primary" icon="terminal" testid="home-new-session" onClick={() => hooks.newSession(false)}>New session</Button>
          <Button icon="plus" testid="home-add-tickets" onClick={() => hooks.addTickets()}>Add tickets</Button>
          <Button icon="chat" testid="home-open-chat" onClick={() => openPanel('chat', 'main', { title: 'Chat' })}>Chat</Button>
          {oc?.url ? <Button icon="external" testid="home-web-chat" title={oc.url} onClick={() => void bridge.shell.openExternal(oc.url)}>Open opencode web</Button> : null}
          <Button icon="findings" onClick={() => openView('findings')}>Findings</Button>
        </div>
      </Card>
      <QueueCard />
      <Card title="Working now" icon="sessions" testid="home-live" actions={<Button size="sm" variant="ghost" onClick={() => openView('sessions')}>All sessions</Button>}>
        {!live.length ? <span class="meta">No session is running.</span> : live.slice(0, 20).map((r) => (
          <div key={r.id} class="list-row" role="button" tabIndex={0} data-testid={`home-live-${r.id}`} onClick={() => openSession(r)} onKeyDown={onEnter(() => openSession(r))}>
            <span class="dot dot--live" aria-label="working" />
            <span class="ellipsis grow">{r.title || r.id}</span>
            {r.ticket ? <span class="mono meta">{r.ticket}</span> : null}
            <span class="meta ellipsis">{basename(r.directory)}</span>
            <span class="meta tnum">{ago(r.updated)}</span>
          </div>
        ))}
      </Card>
      <Card title="Usage, last 24 h" icon="layers" testid="home-usage" wide>
        {!usage ? <span class="meta">No usage recorded yet (the model proxy's ledger is empty or absent).</span> : (
          <div class="col home-gap">
            <div class="home-tiles tnum">{usage.tiles.map((t) => <div key={t.label} class="home-stat"><span class="meta">{t.label}</span><b>{n(t.value)}</b></div>)}</div>
            <span class="meta">Last hour: {n(usage.hour.total)} tokens in {n(usage.hour.replies)} {usage.hour.replies === 1 ? 'reply' : 'replies'}. Reasoning {tokens(usage.reasoning)} and cached prompt {tokens(usage.cached)} tokens are inside the totals.</span>
            {usage.models.length ? (
              <div class="home-scroll-x"><table class="home-models tnum">
                <thead><tr><th>Model</th><th>Replies</th><th>Prompt</th><th>Completion</th><th>Total</th></tr></thead>
                <tbody>{usage.models.map((m) => (
                  <tr key={m.name}><td><span class="mono ellipsis">{m.name}</span><span class="home-bar" style={{ width: `${m.pct}%` }} /></td>
                    <td>{n(m.replies)}</td><td>{n(m.prompt)}</td><td>{n(m.completion)}</td><td>{n(m.total)}</td></tr>
                ))}</tbody>
              </table></div>
            ) : null}
            {usage.remaining ? <span class="meta">Upstream remaining (latest reply): {usage.remaining}</span> : null}
          </div>
        )}
      </Card>
      {st?.vault ? <Pages h={h} /> : null}
      {st?.vault ? (
        <Card title="Vault activity" icon="vault" testid="home-activity">
          {!activity.length ? <span class="meta">No vault commits yet.</span> : (
            <ul class="home-log">{activity.map((a) => (
              <li key={a.sha + a.subject}><span class="ellipsis">{a.subject}</span><span class="meta"><span class="mono">{a.sha}</span> · {a.author} · {a.when}</span></li>
            ))}</ul>
          )}
        </Card>
      ) : null}
      {st?.modes?.length ? (
        <Card title="Prompt modes" icon="settings" testid="home-modes">
          <span class="meta">Each mode is its own entry in opencode's model picker.</span>
          <ul class="home-log">{st.modes.map((m) => <li key={m.name}><span class="mono">{m.name}</span><span class="meta">{m.description}</span></li>)}</ul>
        </Card>
      ) : null}
    </div>
  );
}

export default function HomeView({ ctx }: { ctx: PanelCtx }) {
  const e = useTopic('home');
  const s = useTopic('status');
  useEffect(() => { ctx.setTitle('Home'); }, []);
  const h = e?.data.value;
  const st = s?.data.value;
  const hello = greeting(new Date().getHours());
  return (
    <div class="view home-view" data-testid="view-home">
      <div class="view-head">
        <h1 class="ellipsis">{hello}</h1>
        {st?.vault ? <Pill tone="accent">vault {st.vault.name}</Pill> : null}
        <span class="grow" />
        <AsOf e={e} />
        <Button size="sm" icon="globe" iconOnly title="Open the web version" onClick={() => openWeb('/', { title: 'Home (web)' })} />
      </div>
      <div class="view-body home-body">
        <Health st={st} />
        {h === undefined ? (
          e?.unavailable.value ? <Body h={{ live: [], usage: null, pages: [], activity: [] }} st={st} />
            : e?.error.value ? <ErrorPanel error={e.error.value} onRetry={() => import('../../core/app').then((m) => m.store.load('home'))} />
              : <div data-testid="home-loading"><Skeleton rows={8} /></div>
        ) : <Body h={h} st={st} />}
      </div>
    </div>
  );
}
