// Keys view (3.11): the OS key vault (bridge.keys). Secrets are write-only: the renderer sends a new secret once and only
// ever shows label + last 4. Lock timing (locksAt) and probe state come from main; nothing is re-derived here.
import { useEffect, useState } from 'preact/hooks';
import type { PanelCtx } from '../../shell/dock';
import { keyState, offlineReason } from '../../core/app';
import { bridge } from '../../core/bridge';
import { toast } from '../../core/toast';
import { daysUntil, dateTime, ago } from '../../core/format';
import { openDialog, closeDialog } from '../../shell/ui';
import { Button, Pill, EmptyState, Field } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import type { KeyKind, KeyMeta, KeyOpResult } from '../../../shared/ipc';

const KINDS: { kind: KeyKind; label: string; hint: string }[] = [
  { kind: 'PROXY_API_KEY', label: 'Proxy API key', hint: 'The key the proxy uses for the upstream model API.' },
  { kind: 'OAI_API_KEY', label: 'OpenAI API key', hint: 'Used by the OpenAI-compatible backend.' },
  { kind: 'TRACKER_TOKEN', label: 'Tracker token', hint: 'Jira / tracker access token.' },
  { kind: 'CHATGPT_COOKIE_STRING', label: 'ChatGPT cookie', hint: 'Session cookie string.' },
  { kind: 'ACCESS_CODE', label: 'Access code', hint: 'The access code for the serve UI.' },
];
const kindLabel = (k: KeyKind) => KINDS.find((x) => x.kind === k)?.label ?? k;

async function run(p: Promise<KeyOpResult>, okMsg?: string): Promise<boolean> {
  try {
    const r = await p;
    keyState.value = r.state;
    if (r.ok) { toast({ kind: r.reverted ? 'warn' : 'ok', title: r.msg || okMsg || 'Done' }); return true; }
    toast({ kind: 'err', title: r.error.message }); return false;
  } catch (e) { toast({ kind: 'err', title: String((e as Error)?.message || e) }); return false; }
}

function AddKey({ kind: k0 }: { kind?: KeyKind }) {
  const [kind, setKind] = useState<KeyKind>(k0 ?? 'PROXY_API_KEY');
  const [label, setLabel] = useState('');
  const [secret, setSecret] = useState('');
  const [team, setTeam] = useState(false);
  const [activate, setActivate] = useState(true);
  const [err, setErr] = useState('');
  const save = async () => {
    setErr('');
    if (!secret.trim()) { setErr('Paste the key.'); return; }
    try {
      const r = await bridge.keys.add({ kind, label: label.trim() || kindLabel(kind), secret: secret.trim(), team, activate });
      keyState.value = r.state;
      if (!r.ok) { setErr(r.error.message); return; }
      setSecret('');
      toast({ kind: 'ok', title: r.msg || 'Key added' });
      closeDialog();
    } catch (e) { setErr(String((e as Error)?.message || e)); }
  };
  return (
    <div class="dialog glass glass--overlay" role="dialog" aria-label="Add a key" data-testid="dialog-add-key"
      onKeyDown={(e) => { if (e.key === 'Enter' && !(e.target instanceof HTMLTextAreaElement)) { e.preventDefault(); void save(); } }}>
      <header><Icon name="keys" />Add a key<span class="spacer" /><Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Close" onClick={closeDialog} /></header>
      <div class="dialog-body col" style={{ gap: 'var(--sp-4)' }}>
        <Field label="Kind" hint={KINDS.find((x) => x.kind === kind)?.hint}>
          <select class="select" value={kind} onChange={(e) => setKind((e.target as HTMLSelectElement).value as KeyKind)}>
            {KINDS.map((x) => <option key={x.kind} value={x.kind}>{x.label}</option>)}
          </select>
        </Field>
        <Field label="Label"><input class="input" value={label} placeholder={kindLabel(kind)} onInput={(e) => setLabel((e.target as HTMLInputElement).value)} /></Field>
        <Field label="Key" hint="Stored in the OS key store; never shown again.">
          <input class="input mono" type="password" autoComplete="off" spellcheck={false} data-testid="add-key-secret" value={secret} autoFocus onInput={(e) => setSecret((e.target as HTMLInputElement).value)} />
        </Field>
        <div class="row" style={{ gap: 'var(--sp-5)' }}>
          <label class="check"><input type="checkbox" checked={activate} onChange={(e) => setActivate((e.target as HTMLInputElement).checked)} />Make it the active key</label>
          <label class="check"><input type="checkbox" checked={team} onChange={(e) => setTeam((e.target as HTMLInputElement).checked)} />Team key</label>
        </div>
        {err ? <div class="notice notice--warn" role="alert" data-testid="add-key-error"><Icon name="alert" />{err}</div> : null}
      </div>
      <footer><span class="spacer" /><Button variant="ghost" onClick={closeDialog}>Cancel</Button><Button variant="primary" icon="plus" testid="add-key-submit" onClick={save}>Add key</Button></footer>
    </div>
  );
}

function probePill(k: KeyMeta) {
  const p = k.probe;
  if (!p) return null;
  const tone = p.state === 'ok' ? 'ok' : p.state === 'locked' ? 'warn' : p.state === 'rejected' ? 'err' : 'default';
  return <span title={`${p.message || p.state} · checked ${ago(p.at)}`}><Pill tone={tone as never}>{p.state === 'ok' ? 'works' : p.state}</Pill></span>;
}

function KeyRow({ k }: { k: KeyMeta }) {
  const d = daysUntil(k.locksAt);
  const exp = daysUntil(k.expiresAt);
  return (
    <div class="key-row" data-testid={`key-row-${k.id}`} data-active={k.active ? '1' : undefined}>
      <span class={`dot ${k.active ? 'dot--ok' : ''}`} />
      <div class="col grow" style={{ minWidth: 0, gap: '2px' }}>
        <div class="row" style={{ gap: 'var(--sp-2)' }}>
          <strong class="ellipsis">{k.label}</strong><span class="mono meta">••••{k.last4}</span>
          {k.active ? <Pill tone="accent">active</Pill> : null}{k.team ? <Pill tone="info">team</Pill> : null}
          {k.superseded ? <Pill tone="default">superseded</Pill> : null}{probePill(k)}
        </div>
        <div class="meta">
          added {dateTime(k.addedAt)}
          {d !== null ? <span class={d <= 2 ? 'key-warn' : ''}> · locks in {d} day{d === 1 ? '' : 's'}</span> : null}
          {exp !== null ? <span class={exp <= 7 ? 'key-warn' : ''}> · expires in {exp} day{exp === 1 ? '' : 's'}</span> : null}
        </div>
      </div>
      <div class="row" style={{ gap: 'var(--sp-1)' }}>
        <Button size="sm" variant="ghost" icon="check" onClick={() => bridge.keys.probe({ id: k.id }).then(() => bridge.keys.list()).then((s) => { keyState.value = s; })}>Test</Button>
        {!k.active ? <Button size="sm" testid={`key-activate-${k.id}`} onClick={() => run(bridge.keys.activate(k.id), 'Key activated')}>Make active</Button> : null}
        <Button size="sm" variant="ghost" iconOnly icon="trash" aria-label={`Remove ${k.label}`} title="Remove"
          onClick={async () => { if (window.confirm(`Remove the key “${k.label}”?`)) await run(bridge.keys.remove(k.id), 'Key removed'); }} />
      </div>
    </div>
  );
}

export default function KeysView({ ctx }: { ctx: PanelCtx }) {
  const st = keyState.value;
  useEffect(() => { ctx.setTitle('Keys'); void bridge.keys.list().then((s) => { keyState.value = s; }).catch(() => {}); }, []);
  const groups = KINDS.map((x) => ({ ...x, keys: (st?.keys ?? []).filter((k) => k.kind === x.kind) }));
  return (
    <div class="view keys-view" data-testid="view-keys">
      <div class="view-head toolbar">
        <Icon name="keys" /><strong>Keys</strong>
        {st ? <span class="meta">{st.secure ? `stored in ${st.backend}` : 'not encrypted: the OS key store is unavailable'}</span> : null}
        <span class="spacer" />
        <Button size="sm" variant="primary" icon="plus" testid="keys-add" onClick={() => openDialog(<AddKey />)}>Add key</Button>
      </div>
      <div class="view-body keys-body">
        {!st ? <div class="empty"><span class="spin" /></div> : null}
        {st && !st.secure ? <div class="notice notice--warn"><Icon name="alert" />The OS key store is not available; keys are kept with weaker protection.</div> : null}
        {st && st.storeInactive ? <div class="notice notice--warn"><Icon name="alert" />The key store is inactive; the backend reads keys from .env.</div> : null}
        {st && st.keys.length === 0 ? <EmptyState icon="keys" text="No keys in the vault yet." action={<Button variant="primary" icon="plus" onClick={() => openDialog(<AddKey />)}>Add key</Button>} /> : null}
        {st ? groups.filter((g) => g.keys.length || st.env[g.kind]?.set).map((g) => (
          <section key={g.kind} class="keys-group">
            <div class="section-title row"><span>{g.label}</span><span class="spacer" />
              {st.env[g.kind]?.set && !st.env[g.kind]?.inVault ? <span class="meta">set in .env ({st.env[g.kind]?.chars} chars)</span> : null}
              <Button size="sm" variant="ghost" icon="plus" onClick={() => openDialog(<AddKey kind={g.kind} />)}>Add</Button>
            </div>
            <div class="card keys-card">{g.keys.length ? g.keys.map((k) => <KeyRow key={k.id} k={k} />) : <div class="meta" style={{ padding: 'var(--sp-3)' }}>Only in .env.</div>}</div>
          </section>
        )) : null}
        {offlineReason.value ? <p class="faint small">The backend is offline; a key change applies when it is back.</p> : null}
      </div>
    </div>
  );
}
