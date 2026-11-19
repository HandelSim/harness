// Settings view (3.12): app-side settings, written through bridge.settings.set (patchSettings). Server-side config
// (the .env rows, harness config) stays on the serve page: "Server settings" opens it in the Web panel.
import { useEffect, useState } from 'preact/hooks';
import type { PanelCtx } from '../../shell/dock';
import { openWeb } from '../../shell/dock';
import { settings, patchSettings, appInfo } from '../../core/app';
import { popover } from '../../shell/ui';
import { Button, Seg, Field } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { ACCENT_PRESETS, type Settings } from '../../../shared/ipc';

const ACCENTS = ACCENT_PRESETS;   // the validated enum (main/settings.ts); one list, so a swatch can never store an invalid value
const SECTIONS = ['Appearance', 'Background', 'Terminal', 'Sessions', 'Queue', 'Backend', 'Updates', 'Notifications'] as const;

function Toggle({ label, value, onChange, hint, testid }: { label: string; value: boolean; onChange: (v: boolean) => void; hint?: string; testid?: string }) {
  return (
    <label class="set-row">
      <span class="col grow"><span>{label}</span>{hint ? <span class="meta">{hint}</span> : null}</span>
      <button type="button" role="switch" class="switch" aria-checked={value} aria-label={label} data-testid={testid} onClick={() => onChange(!value)} />
    </label>
  );
}
function Row({ label, hint, children }: { label: string; hint?: string; children: preact.ComponentChildren }) {
  return <div class="set-row"><span class="col grow"><span>{label}</span>{hint ? <span class="meta">{hint}</span> : null}</span><div class="set-ctl">{children}</div></div>;
}
function Num({ value, min, max, step = 1, onChange, label }: { value: number; min: number; max: number; step?: number; onChange: (v: number) => void; label: string }) {
  const [v, setV] = useState(String(value));
  useEffect(() => { setV(String(value)); }, [value]);
  return <input class="input tnum" style={{ width: '84px' }} type="number" aria-label={label} min={min} max={max} step={step} value={v}
    onInput={(e) => setV((e.target as HTMLInputElement).value)}
    onChange={() => { const n = Number(v); if (Number.isFinite(n)) onChange(Math.min(max, Math.max(min, n))); else setV(String(value)); }} />;
}

export default function SettingsView({ ctx }: { ctx: PanelCtx }) {
  const s = settings.value;
  const [sec, setSec] = useState<(typeof SECTIONS)[number]>('Appearance');
  useEffect(() => { ctx.setTitle('Settings'); }, []);
  if (!s) return <div class="view" data-testid="view-settings"><div class="empty"><span class="spin" /></div></div>;
  const set = <K extends keyof Settings>(k: K, p: Partial<Settings[K]>) => void patchSettings({ [k]: p } as never);
  const a = s.appearance;
  return (
    <div class="view settings-view" data-testid="view-settings">
      <nav class="settings-nav" aria-label="Settings sections">
        {SECTIONS.map((x) => <button key={x} class="sidebar-item" aria-current={x === sec ? 'page' : undefined} onClick={() => setSec(x)}>{x}</button>)}
        <span class="grow" />
        <button class="sidebar-item" onClick={() => openWeb('/settings', { title: 'Server settings' })}><Icon name="globe" size={14} />Server settings</button>
        {appInfo.value ? <span class="meta settings-ver">Harness Desktop {appInfo.value.version}</span> : null}
      </nav>
      <div class="settings-body view-body">
        <h2 class="settings-h">{sec}</h2>
        {sec === 'Appearance' ? (
          <div class="card set-card">
            <Row label="Theme"><Seg label="Theme" value={a.theme} onChange={(v) => set('appearance', { theme: v })} options={[{ value: 'dark', label: 'Dark' }, { value: 'light', label: 'Light' }, { value: 'system', label: 'System' }]} /></Row>
            <Row label="Accent">
              <div class="swatches" role="radiogroup" aria-label="Accent">
                {ACCENTS.map((c) => <button key={c} role="radio" aria-checked={a.accent === c} aria-label={c} title={c} class="swatch" data-swatch={c} onClick={() => set('appearance', { accent: c })} />)}
              </div>
            </Row>
            <Row label="Density"><Seg label="Density" value={a.density} onChange={(v) => set('appearance', { density: v })} options={[{ value: 'compact', label: 'Compact' }, { value: 'comfortable', label: 'Comfortable' }]} /></Row>
            <Toggle label="Glass" hint="Translucent overlays (palette, dialogs, menus)." value={a.glass} onChange={(v) => set('appearance', { glass: v })} />
            <Row label="Reduce motion"><Seg label="Reduce motion" value={a.reduceMotion} onChange={(v) => set('appearance', { reduceMotion: v })} options={[{ value: 'system', label: 'System' }, { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }]} /></Row>
            <Row label="Interface scale"><Num label="Interface scale" value={a.uiScale} min={0.8} max={1.5} step={0.05} onChange={(v) => set('appearance', { uiScale: v })} /></Row>
          </div>
        ) : sec === 'Background' ? (
          <div class="card set-card">
            <Row label="Background" hint={s.background.config.id}><Button size="sm" icon="layers" onClick={() => { popover.value = 'bg'; }}>Choose…</Button></Row>
            <Row label="When the window is not focused"><Seg label="On blur" value={s.background.onBlur} onChange={(v) => set('background', { onBlur: v })} options={[{ value: 'run', label: 'Keep running' }, { value: 'dim', label: 'Dim' }, { value: 'pause', label: 'Pause' }]} /></Row>
            <Row label="On battery" hint="Static stops the animation on battery power and keeps the last frame."><Seg label="On battery" value={s.background.onBattery} onChange={(v) => set('background', { onBattery: v })} options={[{ value: 'run', label: 'Keep running' }, { value: 'static', label: 'Static' }]} /></Row>
          </div>
        ) : sec === 'Terminal' ? (
          <div class="card set-card">
            <Field label="Font family"><input class="input mono" value={s.terminal.fontFamily} onChange={(e) => set('terminal', { fontFamily: (e.target as HTMLInputElement).value })} /></Field>
            <Row label="Font size"><Num label="Font size" value={s.terminal.fontSize} min={8} max={32} onChange={(v) => set('terminal', { fontSize: v })} /></Row>
            <Row label="Line height"><Num label="Line height" value={s.terminal.lineHeight} min={1} max={2} step={0.05} onChange={(v) => set('terminal', { lineHeight: v })} /></Row>
            <Row label="Scrollback lines"><Num label="Scrollback" value={s.terminal.scrollback} min={1000} max={200000} step={1000} onChange={(v) => set('terminal', { scrollback: v })} /></Row>
            <Toggle label="Cursor blink" value={s.terminal.cursorBlink} onChange={(v) => set('terminal', { cursorBlink: v })} />
            <Toggle label="Confirm closing a running terminal" value={s.terminal.confirmClose} onChange={(v) => set('terminal', { confirmClose: v })} />
            <Toggle label="Confirm multi-line paste" value={s.terminal.confirmPaste} onChange={(v) => set('terminal', { confirmPaste: v })} />
            <Toggle label="GPU rendering (WebGL)" value={s.terminal.webgl} onChange={(v) => set('terminal', { webgl: v })} />
          </div>
        ) : sec === 'Sessions' ? (
          <div class="card set-card">
            <Row label="On launch"><Seg label="Restore" value={s.sessions.restore} onChange={(v) => set('sessions', { restore: v })} options={[{ value: 'tabs', label: 'Restore tabs' }, { value: 'tabs-and-resume', label: 'Tabs + resume' }, { value: 'none', label: 'Nothing' }]} /></Row>
            <Row label="New session kind"><Seg label="Default kind" value={s.sessions.defaultKind} onChange={(v) => set('sessions', { defaultKind: v })} options={[{ value: 'agent', label: 'Agent' }, { value: 'host', label: 'Host agent' }, { value: 'shell', label: 'Shell' }, { value: 'plain', label: 'Plain' }]} /></Row>
          </div>
        ) : sec === 'Queue' ? (
          <div class="card set-card">
            <Row label="Start the queue" hint="The Start button's default. Host mode always asks first."><Seg label="Start mode" value={s.queue.startMode} onChange={(v) => set('queue', { startMode: v })} options={[{ value: 'container', label: 'Container' }, { value: 'host', label: 'Host' }, { value: 'single', label: 'Single message' }]} /></Row>
          </div>
        ) : sec === 'Backend' ? (
          <div class="card set-card">
            <Row label="Install folder" hint={s.backend.installRoot || 'found automatically'}><Button size="sm" icon="server" onClick={() => { popover.value = 'backend'; }}>Backend…</Button></Row>
            <Row label="Port"><Num label="Port" value={s.backend.port} min={1024} max={65535} onChange={(v) => set('backend', { port: v })} /></Row>
            <Row label="On quit"><Seg label="On quit" value={s.backend.onQuit} onChange={(v) => set('backend', { onQuit: v })} options={[{ value: 'keep-if-busy', label: 'Keep if busy' }, { value: 'stop', label: 'Stop' }, { value: 'keep', label: 'Keep' }]} /></Row>
          </div>
        ) : sec === 'Updates' ? (
          <div class="card set-card">
            <Toggle label="Update the app automatically" value={s.updates.auto} onChange={(v) => set('updates', { auto: v })} />
            <Toggle label="Check for harness updates" value={s.updates.harnessCheck} onChange={(v) => set('updates', { harnessCheck: v })} />
          </div>
        ) : (
          <div class="card set-card">
            <Toggle label="A ticket needs answers or plan review" value={s.notifications.needsInput} onChange={(v) => set('notifications', { needsInput: v })} />
            <Toggle label="A ticket is done" value={s.notifications.ticketDone} onChange={(v) => set('notifications', { ticketDone: v })} />
            <Toggle label="A queue run ended" value={s.notifications.runEnded} onChange={(v) => set('notifications', { runEnded: v })} />
            <Toggle label="A key is about to lock" value={s.notifications.key} onChange={(v) => set('notifications', { key: v })} />
            <Toggle label="An update is ready" value={s.notifications.update} onChange={(v) => set('notifications', { update: v })} />
          </div>
        )}
      </div>
    </div>
  );
}
