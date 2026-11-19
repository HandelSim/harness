// Shared UI primitives: Button (owns its pending state), GateButton (server Gate -> enabled/reason),
// Pill, EmptyState, ErrorPanel, Skeleton, Unavailable, AsOf, Tabs, Seg, Field.
import type { ComponentChildren, ButtonHTMLAttributes } from 'preact';
import { useState, useRef } from 'preact/hooks';
import type { Gate } from '../../shared/api';
import type { TopicEntry } from '../core/store';
import { Icon } from './Icon';
import { clock } from '../core/format';
import { offlineReason } from '../core/app';

type BtnProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onClick' | 'icon' | 'size'> & {
  variant?: 'primary' | 'danger' | 'ghost' | 'default'; size?: 'sm' | 'md'; icon?: string; iconOnly?: boolean;
  onClick?: (e: MouseEvent) => unknown; disabled?: boolean; title?: string; testid?: string; children?: ComponentChildren;
};
/** A button that disables itself while its async onClick runs (double-submit guard, W5). */
export function Button({ variant = 'default', size = 'md', icon, iconOnly, onClick, disabled, children, testid, class: cls, ...rest }: BtnProps) {
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const click = async (e: MouseEvent) => {
    if (lock.current || disabled) return;
    const r = onClick?.(e);
    if (r && typeof (r as Promise<unknown>).then === 'function') {
      lock.current = true; setBusy(true);
      try { await r; } finally { lock.current = false; setBusy(false); }
    }
  };
  const c = ['btn', variant !== 'default' ? `btn--${variant}` : '', size === 'sm' ? 'btn--sm' : '', iconOnly ? 'btn--icon' : '', cls ?? ''].join(' ');
  return (
    <button type="button" class={c} disabled={disabled || busy} aria-busy={busy || undefined} data-testid={testid} onClick={click} {...rest}>
      {busy ? <span class="spin" /> : icon ? <Icon name={icon} size={size === 'sm' ? 14 : 16} /> : null}
      {iconOnly ? null : children}
    </button>
  );
}

/** Button driven by a server Gate: disabled with the server's reason as tooltip. Offline disables too. */
export function GateButton({ gate, title, ...p }: BtnProps & { gate: Gate | undefined }) {
  const off = offlineReason.value;
  const g = gate ?? { ok: false, reason: 'not available' };
  const reason = off || (!g.ok ? g.reason : '');
  return <Button {...p} disabled={!!reason || p.disabled} title={reason || title} aria-disabled={!!reason || undefined} />;
}

export function Pill({ tone = 'default', children, dot, testid }: { tone?: 'ok' | 'warn' | 'err' | 'info' | 'accent' | 'default'; children: ComponentChildren; dot?: 'live' | 'ok' | 'warn' | 'err'; testid?: string }) {
  return <span class={`pill ${tone !== 'default' ? `pill--${tone}` : ''}`} data-testid={testid}>{dot ? <span class={`dot dot--${dot}`} /> : null}{children}</span>;
}

export function EmptyState({ icon = 'sparkle', text, action }: { icon?: string; text: string; action?: ComponentChildren }) {
  return (
    <div class="empty enter" data-testid="empty-state">
      <div class="empty-icon"><Icon name={icon} size={22} /></div>
      <p>{text}</p>
      {action}
    </div>
  );
}

export function ErrorPanel({ error, onRetry }: { error: { code: string; message: string }; onRetry?: () => unknown }) {
  const details = `${error.code}: ${error.message}`;
  return (
    <div class="error-panel enter" data-testid="error-panel" role="alert">
      <div class="row"><Icon name="alert" /><strong>Could not load</strong><span class="meta">{error.code}</span></div>
      <div class="msg">{error.message}</div>
      <div class="row">
        {onRetry ? <Button size="sm" icon="refresh" onClick={onRetry}>Retry</Button> : null}
        <Button size="sm" variant="ghost" icon="copy" onClick={() => navigator.clipboard?.writeText(details)}>Copy details</Button>
      </div>
    </div>
  );
}

export function Skeleton({ rows = 6 }: { rows?: number }) {
  return <div class="col" style={{ padding: 'var(--sp-3)' }} aria-busy="true">{Array.from({ length: rows }, (_, i) => <div key={i} class="skeleton" style={{ height: '20px', width: `${90 - (i * 13) % 40}%` }} />)}</div>;
}

/** "Not available on this harness version" + Open web version (PLAN 4.6). */
export function Unavailable({ webPath, openWeb }: { webPath: string; openWeb: (path: string) => void }) {
  return (
    <EmptyState icon="globe" text="This view is not available natively on this harness version."
      action={<Button variant="primary" icon="external" onClick={() => openWeb(webPath)}>Open web version</Button>} />
  );
}

export function AsOf({ e }: { e: TopicEntry<unknown> | null }) {
  if (!e || !e.at.value) return null;
  return <span class="asof tnum" data-stale={e.stale.value ? '1' : undefined}>{e.stale.value ? `as of ${clock(e.at.value)}` : `updated ${clock(e.at.value)}`}</span>;
}

/**
 * Standard topic body: skeleton on first load, error panel (never blocking), unavailable fallback, else children.
 * Live updates never clear: once data exists, errors only mark it stale.
 */
export function TopicBody<T>({ e, webPath, openWeb, children }: { e: TopicEntry<T> | null; webPath: string; openWeb: (p: string) => void; children: (d: T) => ComponentChildren }) {
  if (!e) return null;
  const d = e.data.value;
  if (d === undefined) {
    if (e.unavailable.value) return <Unavailable webPath={webPath} openWeb={openWeb} />;
    if (e.error.value) return <ErrorPanel error={e.error.value} onRetry={() => import('../core/app').then((m) => m.store.load(e.topic))} />;
    return <Skeleton />;
  }
  return <>{children(d)}</>;
}

export function Tabs<T extends string>({ tabs, value, onChange, testidPrefix }: { tabs: { id: T; label: string; badge?: ComponentChildren }[]; value: T; onChange: (t: T) => void; testidPrefix?: string }) {
  const onKey = (e: KeyboardEvent) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const i = tabs.findIndex((t) => t.id === value);
    const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    onChange(n.id);
    e.preventDefault();
  };
  return (
    <div class="tabs" role="tablist" onKeyDown={onKey}>
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={t.id === value} tabIndex={t.id === value ? 0 : -1}
          data-testid={testidPrefix ? `${testidPrefix}${t.id}` : undefined} onClick={() => onChange(t.id)}>
          {t.label}{t.badge ? <span class="badge">{t.badge}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function Seg<T extends string>({ options, value, onChange, label }: { options: { value: T; label: string }[]; value: T; onChange: (v: T) => void; label?: string }) {
  return (
    <div class="seg" role="group" aria-label={label}>
      {options.map((o) => <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>{o.label}</button>)}
    </div>
  );
}

export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: ComponentChildren }) {
  return <div class="field"><label>{label}</label>{children}{hint ? <span class="hint">{hint}</span> : null}{error ? <span class="err">{error}</span> : null}</div>;
}
