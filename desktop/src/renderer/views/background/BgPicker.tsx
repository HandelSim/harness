// Background picker (W19, 3.17): grid of thumbnails grouped by kind, hover previews, click commits;
// intensity / speed / fps / seed and the per-background params.
import { useEffect, useRef, useState } from 'preact/hooks';
import type { BgConfig, BgDescriptor } from '../../../shared/bg';
import { bgCatalog, bgEngine, bgStats, bgError, currentBgTheme, previewBg, bgLowPower, setLowPower, lowPowerPlan, applySurfaceAlphas, type LowPowerMode } from '../../core/background';
import { settings, patchSettings } from '../../core/app';
import { popover } from '../../shell/ui';
import { Button, Seg } from '../../ui/kit';

const GROUPS: Record<string, string> = { static: 'Static', flow: 'Flow', chaos: 'Chaos', automata: 'Automata', fields: 'Fields', system: 'System' };

function Thumb({ d }: { d: BgDescriptor }) {
  const [url, setUrl] = useState('');
  useEffect(() => {
    let alive = true;
    bgEngine()?.thumbnail(d.id, 160, 100, currentBgTheme()).then((u) => { if (alive) setUrl(u); }, () => {});
    return () => { alive = false; };
  }, [d.id, document.documentElement.dataset.theme]);
  return <div class="bg-thumb" style={url ? { backgroundImage: `url("${url}")` } : undefined} />;
}

export function BgPicker() {
  const el = useRef<HTMLDivElement>(null);
  const cat = bgCatalog.value;
  const cfg = settings.value?.background?.config;
  useEffect(() => {
    const off = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (el.current && !el.current.contains(t) && !t.closest?.('[data-testid="status-bg"]')) popover.value = null;
    };
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') { previewBg(null); popover.value = null; } };
    window.addEventListener('pointerdown', off, true); window.addEventListener('keydown', esc);
    return () => { window.removeEventListener('pointerdown', off, true); window.removeEventListener('keydown', esc); previewBg(null); };
  }, []);
  const commit = (next: Partial<BgConfig>) => {
    if (!cfg) return;
    const merged: BgConfig = { ...cfg, ...next };
    void patchSettings({ background: { config: merged } } as never);
  };
  const pick = (d: BgDescriptor) => {
    const params: BgConfig['params'] = {};
    for (const p of d.params) params[p.key] = cfg?.id === d.id ? (cfg.params[p.key] ?? p.default) : p.default;
    commit({ id: d.id, params });
  };
  const desc = cat.find((d) => d.id === cfg?.id);
  const lp = bgLowPower.value;
  const see = settings.value?.appearance?.seeThrough ?? 0.5;
  const groups = [...new Set(cat.map((d) => d.group))];
  return (
    <div ref={el} class="popover glass glass--overlay bg-picker" data-testid="bg-picker" role="dialog" aria-label="Background"
      style={{ right: '12px', bottom: 'calc(var(--status-h) + 8px)', width: 'min(560px, calc(100vw - 24px))', maxWidth: 'none', maxHeight: '70vh', overflow: 'auto' }}>
      <div class="row"><strong>Background</strong><span class="spacer" />
        {bgStats.value ? <span class="meta tnum">{bgStats.value.renderer} · {Math.round(bgStats.value.fps)} fps{bgStats.value.downgraded ? ` · ${bgStats.value.reason}` : ''}</span> : null}</div>
      {bgError.value ? <div class="notice notice--warn small">Background engine unavailable: {bgError.value}</div> : null}
      {lp.active ? (
        <div class="notice small" data-testid="bg-lowpower" style={{ margin: 'var(--sp-2) 0' }}>
          Low-power mode{lp.gpu?.software ? ` (no GPU: ${lp.gpu.renderer || 'software rendering'})` : ''}: heavy backgrounds draw a still
          frame, the rest run at lower resolution and frame rate.
        </div>
      ) : null}
      {groups.map((g) => (
        <section key={g}>
          <div class="section-title">{GROUPS[g] ?? g}</div>
          <div class="bg-grid">
            {cat.filter((d) => d.group === g).map((d) => (
              <button key={d.id} class="bg-item" data-testid={`bg-item-${d.id}`} aria-pressed={cfg?.id === d.id} title={d.blurb}
                onMouseEnter={() => cfg && previewBg({ ...cfg, id: d.id, params: Object.fromEntries(d.params.map((p) => [p.key, p.default])) })}
                onMouseLeave={() => previewBg(null)} onFocus={() => cfg && previewBg({ ...cfg, id: d.id, params: Object.fromEntries(d.params.map((p) => [p.key, p.default])) })}
                onBlur={() => previewBg(null)} onClick={() => pick(d)}>
                <Thumb d={d} />
                <span class="ellipsis">{d.name}</span>
                {lp.active && lowPowerPlan(d)?.mode === 'static'
                  ? <span class="meta" data-testid={`bg-still-${d.id}`} title="Low-power mode draws a still frame of this one">still</span>
                  : d.cost >= 3 ? <span class="meta" title="GPU heavy">●●●</span> : null}
              </button>
            ))}
          </div>
        </section>
      ))}
      {cfg ? (
        <div class="col" style={{ gap: 'var(--sp-2)', marginTop: 'var(--sp-3)' }}>
          <label class="row small">Intensity<input type="range" class="grow" min={0} max={1} step={0.05} value={cfg.intensity} onChange={(e) => commit({ intensity: +(e.target as HTMLInputElement).value })} /></label>
          {desc?.dynamic ? <label class="row small">Speed<input type="range" class="grow" min={0} max={2} step={0.1} value={cfg.speed} onChange={(e) => commit({ speed: +(e.target as HTMLInputElement).value })} /></label> : null}
          {desc?.dynamic ? <div class="row small">FPS cap<Seg options={[15, 24, 30, 60].map((v) => ({ value: String(v), label: String(v) }))} value={String(cfg.fpsCap)} onChange={(v) => commit({ fpsCap: +v as BgConfig['fpsCap'] })} /></div> : null}
          {desc?.params.map((p) => (
            <label key={p.key} class="row small">{p.label}
              {p.type === 'range' ? <input type="range" class="grow" min={p.min} max={p.max} step={p.step} value={Number(cfg.params[p.key] ?? p.default)}
                onChange={(e) => commit({ params: { ...cfg.params, [p.key]: +(e.target as HTMLInputElement).value } })} />
                : p.type === 'toggle' ? <input type="checkbox" checked={!!(cfg.params[p.key] ?? p.default)} onChange={(e) => commit({ params: { ...cfg.params, [p.key]: (e.target as HTMLInputElement).checked } })} />
                : <select class="select" value={String(cfg.params[p.key] ?? p.default)} onChange={(e) => commit({ params: { ...cfg.params, [p.key]: (e.target as HTMLSelectElement).value } })}>
                    {p.options?.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select>}
            </label>
          ))}
          <div class="row">{desc?.dynamic ? <Button size="sm" icon="refresh" onClick={() => commit({ seed: Math.floor(Math.random() * 2 ** 31) })}>New seed</Button> : null}</div>
        </div>
      ) : null}
      <div class="col" style={{ gap: 'var(--sp-2)', marginTop: 'var(--sp-3)' }}>
        <label class="row small" title="How much of the background shows through the window surfaces. Text contrast stays at least 4.5:1.">
          See-through<input type="range" class="grow" data-testid="bg-see-through" min={0} max={1} step={0.05} value={see}
            onInput={(e) => applySurfaceAlphas(+(e.target as HTMLInputElement).value)}
            onChange={(e) => void patchSettings({ appearance: { seeThrough: +(e.target as HTMLInputElement).value } } as never)} /></label>
        <div class="row small" title="Auto turns low power on when the GPU is software-only">Low power
          <Seg options={[{ value: 'auto', label: 'Auto' }, { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }]} value={lp.mode}
            onChange={(v) => setLowPower(v as LowPowerMode)} /></div>
      </div>
    </div>
  );
}
