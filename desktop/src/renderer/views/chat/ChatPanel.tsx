// Chat panel: the native stand-in for serve's chat (opencode web). Left: sessions from the `sessions` topic, live
// (messageable) runs first; right: the session's conversation, the transcript panel embedded as is (its composer
// posts /sessions/<sid>/send when the session's send gate is open). Host sessions also open in opencode web.
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { signal } from '@preact/signals';
import type { SessionRow } from '../../../shared/api';
import { store, useTopic } from '../../core/app';
import { bridge } from '../../core/bridge';
import { ago } from '../../core/format';
import { isTyping } from '../../core/keymap';
import { openWeb, type PanelCtx } from '../../shell/dock';
import { VIEW_WEB } from '../../shell/panels';
import { hooks } from '../../shell/commands';
import { AsOf, Button, EmptyState, ErrorPanel, Seg, Skeleton } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { VirtualList } from '../queue/virtual';
import TranscriptPanel from '../transcript/TranscriptPanel';
import { chatRows, pickSession, rowMeta, step, type Scope } from './model';
import css from './chat.css' with { type: 'text' };

if (!document.getElementById('css-chat')) { const el = document.createElement('style'); el.id = 'css-chat'; el.textContent = css as string; document.head.append(el); }

const ROW_H = 46;
const CONV_DEBOUNCE_MS = 150;

/** The transcript's ctx: the chat panel's own, keyed by the session, its title and tab state mapped onto the chat tab. */
function useSubCtx(ctx: PanelCtx, sid: string): PanelCtx {
  return useMemo<PanelCtx>(() => ({
    ...ctx, key: sid, params: signal({ sid }),
    setTitle: (t: string) => ctx.setTitle(t ? `Chat · ${t}` : 'Chat'),
    update: () => {}, close: () => {}, beforeClose: () => {},
  }), [ctx, sid]);
}

function Conversation({ ctx, sid, row }: { ctx: PanelCtx; sid: string; row: SessionRow | undefined }) {
  const sub = useSubCtx(ctx, sid);
  const st = useTopic('status');
  const oc = st?.data.value?.opencode?.url;
  const web = row?.web || null;
  return (
    <div class="chat-conv" data-testid={`chat-conv-${sid}`}>
      {row && !row.live ? (
        <div class="notice chat-notice" data-testid="chat-readonly">
          <Icon name="info" size={14} />
          <span class="grow">{row.source === 'host' ? 'This host session is not a live run: reply to it in opencode web.' : 'This run is not live (finished, or not a headless run): read only.'}</span>
          {web || (row.source === 'host' && oc) ? <Button size="sm" icon="external" testid="chat-open-oc" onClick={() => void bridge.shell.openExternal(web || oc!)}>Open in opencode web</Button> : null}
        </div>
      ) : null}
      <div class="chat-transcript"><TranscriptPanel key={sid} ctx={sub} /></div>
    </div>
  );
}

export default function ChatPanel({ ctx }: { ctx: PanelCtx }) {
  const e = useTopic('sessions');
  const st = useTopic('status');
  const p = ctx.params.value as { sid?: string; scope?: Scope };
  const [q, setQ] = useState('');
  const [scope, setScopeS] = useState<Scope>(p.scope === 'live' ? 'live' : 'all');
  const [sid, setSidS] = useState(typeof p.sid === 'string' ? p.sid : '');
  const listRef = useRef<HTMLDivElement>(null);
  const rows = e?.data.value;
  const list = useMemo(() => chatRows(rows, q, scope), [rows, q, scope]);
  const cur = pickSession(list, sid);
  const row = list.find((r) => r.id === cur);
  const setSid = (s: string) => { setSidS(s); ctx.update({ sid: s }); };
  // the conversation follows the selection after a short pause, so holding an arrow key through the list does not
  // mount (and page in) a transcript per row; a click shows it at once (P2-5)
  const [convSid, setConvSid] = useState(cur);
  useEffect(() => { if (cur === convSid) return; const t = setTimeout(() => setConvSid(cur), CONV_DEBOUNCE_MS); return () => clearTimeout(t); }, [cur]);
  const convRow = list.find((r) => r.id === convSid) ?? (convSid === cur ? row : undefined);
  const setScope = (s: Scope) => { setScopeS(s); ctx.update({ scope: s }); };
  useEffect(() => { if (!cur) ctx.setTitle('Chat'); }, [cur]);
  const onKey = (ev: KeyboardEvent) => {
    if (isTyping(ev.target)) return;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); setSid(step(list, cur, ev.key === 'ArrowDown' ? 1 : -1)); }
    else if (ev.key === 'Home' || ev.key === 'End') { ev.preventDefault(); if (list.length) setSid(list[ev.key === 'Home' ? 0 : list.length - 1].id); }
    else if (ev.key === 'Enter' && cur) {
      ev.preventDefault();
      (listRef.current?.closest('.chat-panel')?.querySelector('.chat-transcript textarea') as HTMLTextAreaElement | null)?.focus();
    }
  };
  const oc = st?.data.value?.opencode?.url;
  return (
    <div class="view chat-panel" data-testid="view-chat" ref={listRef}>
      <div class="chat-split">
        <aside class="chat-side">
          <div class="chat-side-head">
            <label class="search-field"><Icon name="search" size={13} /><input class="input input--bare" placeholder="Find a session" aria-label="Find a session" data-testid="chat-filter" value={q} onInput={(ev) => setQ((ev.target as HTMLInputElement).value)} /></label>
            <div class="row">
              <Seg<Scope> label="Show" value={scope} onChange={setScope} options={[{ value: 'all', label: 'All' }, { value: 'live', label: 'Live' }]} />
              <span class="grow" />
              <AsOf e={e} />
              <Button size="sm" icon="plus" iconOnly title="New session" onClick={() => hooks.newSession(false)} />
              {oc ? <Button size="sm" icon="external" iconOnly title="Open opencode web (new chats live there)" testid="chat-oc-web" onClick={() => void bridge.shell.openExternal(oc)} /> : null}
            </div>
          </div>
          {rows === undefined ? (
            e?.unavailable.value ? <EmptyState icon="globe" text="This serve does not list sessions to the app yet." action={<Button icon="globe" onClick={() => openWeb(VIEW_WEB.sessions)}>Open in Web panel</Button>} />
              : e?.error.value ? <ErrorPanel error={e.error.value} onRetry={() => store.load('sessions')} />
                : <div data-testid="chat-loading" class="chat-pad"><Skeleton rows={6} /></div>
          ) : !list.length ? (
            <EmptyState icon="chat" text={q ? 'No session matches.' : scope === 'live' ? 'No live run to talk to. Runs started by the queue or New session show up here while they work.' : 'No sessions yet.'}
              action={!q ? <Button variant="primary" icon="terminal" onClick={() => hooks.newSession(false)}>New session</Button> : undefined} />
          ) : (
            <VirtualList class="chat-list" role="listbox" aria-label="Sessions" tabIndex={0} data-testid="chat-list" onKeyDown={onKey}
              aria-activedescendant={cur ? `chat-row-${cur}` : undefined} items={list} rowHeight={ROW_H} focusIndex={list.findIndex((r) => r.id === cur)}
              row={(r: SessionRow) => (
                <div key={r.id} id={`chat-row-${r.id}`} class="chat-row" role="option" aria-selected={r.id === cur} data-testid={`chat-row-${r.id}`} onClick={() => { setSid(r.id); setConvSid(r.id); }}>
                  <span class={`dot ${r.live ? (r.working ? 'dot--live' : 'dot--ok') : ''}`} aria-label={r.live ? 'live' : 'ended'} />
                  <span class="chat-row-text">
                    <span class="ellipsis chat-row-title">{r.title || r.id}</span>
                    <span class="meta ellipsis">{rowMeta(r)}</span>
                  </span>
                  <span class="meta tnum">{ago(r.updated)}</span>
                </div>
              )} />
          )}
        </aside>
        <section class="chat-main">
          {cur && convSid ? <Conversation ctx={ctx} sid={convSid} row={convRow} />
            : <EmptyState icon="chat" text="Pick a session to read it and, while it runs, talk to it." />}
        </section>
      </div>
    </div>
  );
}
