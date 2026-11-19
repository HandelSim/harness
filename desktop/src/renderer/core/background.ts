// The one place that talks to the background engine (L4). Guarded: if the engine fails to create,
// the shell still runs with the plain token background.
import { signal, effect } from '@preact/signals';
import type { BgConfig, BgEngine, BgStats, BgTheme, BgDescriptor } from '../../shared/bg';
import { settings, windowState, reducedMotion } from './app';
import { surfaceAlphas } from '../views/background/surface';

export const bgStats = signal<BgStats | null>(null);
export const bgCatalog = signal<BgDescriptor[]>([]);
export const bgError = signal<string>('');
/** No-GPU policy (W2-G): 'auto' turns low power on when the GL renderer is software. Per device, so localStorage. */
export type LowPowerMode = 'auto' | 'on' | 'off';
export interface LowPowerState { mode: LowPowerMode; active: boolean; gpu: { renderer: string; software: boolean } | null }
export const bgLowPower = signal<LowPowerState>({ mode: 'auto', active: false, gpu: null });
const LP_KEY = 'harness.bg.lowPower';
function readLowPower(): LowPowerMode {
  try { const v = localStorage.getItem(LP_KEY); return v === 'on' || v === 'off' ? v : 'auto'; } catch { return 'auto'; }
}
type LowPowerEngine = BgEngine & { setLowPower?(m: LowPowerMode): void; lowPower?(): LowPowerState };
function syncLowPower(): void {
  const e = engine as LowPowerEngine | null;
  const st = e?.lowPower?.();
  if (st) bgLowPower.value = { mode: st.mode, active: !!st.active, gpu: st.gpu ?? null };
}
export function setLowPower(mode: LowPowerMode): void {
  try { if (mode === 'auto') localStorage.removeItem(LP_KEY); else localStorage.setItem(LP_KEY, mode); } catch { /* per-device nicety only */ }
  (engine as LowPowerEngine | null)?.setLowPower?.(mode);
  bgLowPower.value = { ...bgLowPower.value, mode };
  syncLowPower();
}

/** Surface opacity: --chrome-alpha-auto / --panel-alpha-auto on <html> from appearance.seeThrough, floored for AA. */
export function applySurfaceAlphas(seeThrough?: number): void {
  const s = settings.value;
  const h = document.documentElement;
  const out = surfaceAlphas({
    seeThrough: seeThrough ?? s?.appearance?.seeThrough ?? 0.5, theme: currentBgTheme(),
    surface: cssVar('--surface-0'), texts: [cssVar('--text-1'), cssVar('--text-2'), cssVar('--text-3')],
  });
  h.style.setProperty('--chrome-alpha-auto', String(out.chrome));
  h.style.setProperty('--panel-alpha-auto', String(out.panel));
}
let engine: BgEngine | null = null;
type PlanFn = (d: BgDescriptor) => { mode: 'full' | 'reduced' | 'static'; fpsCap: number; scaleMul: number };
let planFn: PlanFn | null = null;
/** What low power does to a background id (null when the engine has no such policy). */
export function lowPowerPlan(d: BgDescriptor): ReturnType<PlanFn> | null { try { return planFn ? planFn(d) : null; } catch { return null; } }
export function bgEngine(): BgEngine | null { return engine; }

function cssVar(name: string): string { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
export function currentBgTheme(): BgTheme {
  return {
    mode: document.documentElement.dataset.theme === 'light' ? 'light' : 'dark',
    base: cssVar('--bg-window') || '#0b0c0f', fg: cssVar('--text-1') || '#e8e8ea',
    accent: cssVar('--accent-9') || '#7c7cff', accent2: cssVar('--accent-11') || '#a5a5ff',
  };
}

export async function startBackground(root: HTMLElement): Promise<void> {
  bindSurfaces();
  try {
    const mod = await import('../../background/index');
    if (typeof mod.createBackground !== 'function') throw new Error('background module is a stub');
    const lp = (mod as unknown as { lowPowerPlan?: PlanFn }).lowPowerPlan;
    if (typeof lp === 'function') planFn = lp;
    engine = mod.createBackground(root, { workerUrl: 'assets/bg-worker.js' });
    (engine as LowPowerEngine).setLowPower?.(readLowPower());
    syncLowPower();
    bgCatalog.value = engine.catalog();
    engine.onStats((s) => { bgStats.value = s; });
    let last = '';
    effect(() => {
      const s = settings.value;
      const cfg: BgConfig = s?.background?.config ?? mod.DEFAULT_BG;
      const key = JSON.stringify(cfg);
      if (key !== last) { const first = !last; last = key; void engine?.set(cfg, { fadeMs: first ? 0 : 600 }).catch((e) => { bgError.value = String(e?.message || e); }); }
    });
    effect(() => {
      void settings.value?.appearance; // re-resolve after theme/accent attribute changes
      requestAnimationFrame(() => { engine?.setTheme(currentBgTheme()); engine?.setReducedMotion(reducedMotion()); });
    });
    effect(() => {
      const w = windowState.value;
      const onBlur = settings.value?.background?.onBlur ?? 'dim';
      engine?.setRunState(!w.visible ? 'pause' : w.focused ? 'run' : onBlur);
    });
  } catch (e) {
    bgError.value = String((e as Error)?.message || e);
    engine = null;
  }
}

/** Runs even when the engine fails: the surfaces still follow seeThrough (the floor then uses the plain base). */
function bindSurfaces(): void {
  effect(() => {
    void settings.value?.appearance;
    requestAnimationFrame(() => applySurfaceAlphas()); // after bindAppearance has written data-theme / data-accent
  });
}
export function previewBg(cfg: BgConfig | null): void { engine?.preview(cfg); }
