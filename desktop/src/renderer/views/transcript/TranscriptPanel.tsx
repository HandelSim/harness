// Transcript panel (3.7): a session's messages, followed while it works. Server text goes through the one Markdown
// component (assistant/user text) or as text (tool input/output). A live update never moves the scroll when the
// reader has scrolled up (a "N new" pill appears instead) and never touches the composer draft.
import { Component } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import type { Message, MessagesPage, SessionHead } from '../../../shared/api';
import { act, useTopic } from '../../core/app';
import { api, bridge, enc } from '../../core/bridge';
import { clock, tokens } from '../../core/format';
import { openWeb, type PanelCtx } from '../../shell/dock';
import { AsOf, Button, GateButton, Pill } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { Markdown } from '../../ui/Markdown';
import { useDraft } from '../ticket/parts';
import { mergePage, nextFrom, partView, tailStart, TAIL } from './model';

const MAX_PAGES = 40;

/** Memoised on message identity: mergePage keeps the object of an unchanged message, so a poll re-renders only what grew. */
class MessageItem extends Component<{ m: Message }> {
  shouldComponentUpdate(n: { m: Message }): boolean { return n.m !== this.props.m; }
  render() { return <MessageView m={this.props.m} />; }
}

function MessageView({ m }: { m: Message }) {
  const user = m.role === 'user';
  const info = m.info as { tokens?: { input?: number; output?: number }; agent?: string };
  return (
    <article class={`tmsg ${user ? 'tmsg--user' : 'tmsg--assistant'}`} data-testid={`msg-${m.index}`}>
      <header class="tmsg-head meta">
        <span class="tmsg-role">{user ? 'You' : String(info.agent || 'Agent')}</span>
        <span class="tnum">{clock(m.created)}</span>
        {info.tokens ? <span class="tnum">{tokens(info.tokens.input)} in · {tokens(info.tokens.output)} out</span> : null}
      </header>
      {m.parts.map((p) => {
        const v = partView(p);
        if (!v) return null;
        if (v.kind === 'text') return <Markdown key={p.id} text={v.text} class="tmsg-text" />;
        if (v.kind === 'reasoning') return <details key={p.id} class="tpart tpart--think"><summary>Thinking</summary><Markdown text={v.text} /></details>;
        if (v.kind === 'tool') return (
          <details key={p.id} class="tpart tpart--tool">
            <summary><Icon name="terminal" size={12} /><span class="mono">{v.name}</span>{v.status ? <Pill tone={v.status === 'error' ? 'err' : v.status === 'completed' ? 'ok' : 'info'}>{v.status}</Pill> : null}{p.truncated ? <span class="meta">truncated</span> : null}</summary>
            {v.input ? <pre class="tpre mono">{v.input}</pre> : null}
            {v.output ? <pre class="tpre mono">{v.output}</pre> : null}
          </details>
        );
        return <div key={p.id} class="meta tpart">{v.label}</div>;
      })}
    </article>
  );
}

export default function TranscriptPanel({ ctx }: { ctx: PanelCtx }) {
  const sid = String(ctx.params.value.sid ?? ctx.key ?? '');
  const head = useTopic(sid ? (`session:${sid}` as const) : null);
  const hd = head?.data.value;
  const h = hd && !('gone' in hd) ? (hd as SessionHead) : null;
  const [msgs, setMsgs] = useState<Message[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [unseen, setUnseen] = useState(0);
  const [shown, setShown] = useState(TAIL);
  const listRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const msgsRef = useRef<Message[]>([]);
  const visible = ctx.visible.value;
  const [draft, setDraft, clearDraft] = useDraft(`session:${sid}`);
  const [sending, setSending] = useState(false);

  useEffect(() => { ctx.setTitle(h?.row.title || sid.slice(0, 12)); }, [h?.row.title, sid]);
  useEffect(() => { ctx.setTabState(working ? 'working' : 'idle'); }, [working]);

  const load = async (): Promise<boolean> => {
    let list = msgsRef.current;
    for (let i = 0; i < MAX_PAGES; i++) {
      const r = await bridge.api.get<MessagesPage>(api(`/sessions/${enc(sid)}/messages`), { from: nextFrom(list) });
      if (!r.ok) { setErr(r.error.message); return false; }
      if (!r.data) break;
      const before = list.length;
      list = mergePage(list, r.data);
      setWorking(r.data.working);
      if (list.length >= r.data.count || list.length === before) break;
    }
    const added = list.length - msgsRef.current.length;
    setErr(null);
    if (list === msgsRef.current) return true;     // the page changed nothing: no render (M7)
    msgsRef.current = list;
    setMsgs(list);
    if (added > 0 && !stick.current) setUnseen((n) => n + added);
    return true;
  };

  useEffect(() => { msgsRef.current = []; setMsgs([]); setShown(TAIL); stick.current = true; if (sid) void load(); }, [sid]);
  useEffect(() => {
    if (!visible || !sid || !(working || h?.row.working || h?.row.live)) return;
    const t = setInterval(() => { void load(); }, 2000);
    return () => clearInterval(t);
  }, [visible, sid, working, h?.row.working, h?.row.live]);
  useEffect(() => {
    const el = listRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [msgs]);

  const onScroll = () => {
    const el = listRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (stick.current) setUnseen(0);
  };
  const jump = () => { const el = listRef.current; if (el) { el.scrollTop = el.scrollHeight; stick.current = true; setUnseen(0); } };
  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    const r = await act(`/sessions/${enc(sid)}/send`, { text }, { quiet: true, refresh: [`session:${sid}`] });
    setSending(false);
    if (r.ok) { clearDraft(); stick.current = true; void load(); }
  };

  if (!sid) return <div class="view"><div class="empty">No session.</div></div>;
  const gone = !!hd && 'gone' in hd;
  return (
    <div class="view transcript" data-testid={`transcript-${sid}`}>
      <div class="view-head">
        <span class="row grow" style={{ minWidth: 0, gap: 'var(--sp-2)' }}>
          {working ? <Pill tone="accent" dot="live">working</Pill> : gone ? <Pill tone="warn">removed</Pill> : <Pill>idle</Pill>}
          <span class="ellipsis">{h?.row.title || sid}</span>
          {h ? <span class="meta mono ellipsis">{h.row.directory}</span> : null}
        </span>
        <AsOf e={head} />
        {h ? <GateButton size="sm" gate={h.stop} icon="stop" onClick={() => void act(`/sessions/${enc(sid)}/stop`, {}, { refresh: [`session:${sid}`] })}>Stop</GateButton> : null}
        <Button size="sm" icon="globe" iconOnly title="Open in the web page" onClick={() => openWeb(`/sessions/${enc(sid)}`, { title: sid.slice(0, 12) })} />
      </div>
      <div class="transcript-list" ref={listRef} onScroll={onScroll}>
        {err && !msgs.length ? <div class="notice notice--warn">{err}</div> : null}
        {!err && !msgs.length ? <div class="empty"><span class="spin" /></div> : null}
        {tailStart(msgs.length, shown) > 0 ? (
          <button class="btn btn--sm btn--ghost transcript-earlier" data-testid="transcript-earlier"
            onClick={() => { stick.current = false; setShown((n) => n + TAIL); }}>Show earlier ({tailStart(msgs.length, shown)})</button>
        ) : null}
        {msgs.slice(tailStart(msgs.length, shown)).map((m) => <MessageItem key={m.id} m={m} />)}
      </div>
      {unseen > 0 ? <button class="tk-newpill" onClick={jump}>{unseen} new</button> : null}
      {h && h.send.ok ? (
        <div class="transcript-compose">
          <textarea class="input" rows={2} placeholder="Message the agent (Ctrl+Enter sends)" value={draft} aria-label="Message"
            onInput={(e) => setDraft((e.target as HTMLTextAreaElement).value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void send(); } }} />
          <Button variant="primary" size="sm" icon="send" disabled={!draft.trim() || sending} onClick={() => void send()}>Send</Button>
        </div>
      ) : h && h.send.reason ? <div class="meta transcript-why">{h.send.reason}</div> : null}
    </div>
  );
}
