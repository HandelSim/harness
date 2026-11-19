// The one place that talks to the background engine (L4). Guarded: if the engine fails to create,
// the shell still runs with the plain token background.
import { signal, effect } from '@preact/signals';
import type { BgConfig, BgEngine, BgStats, BgTheme, BgDescriptor } from '../../shared/bg';
import { settings, windowState, reducedMotion } from './app';

export const bgStats = signal<BgStats | null>(null);
export const bgCatalog = signal<BgDescriptor[]>([]);
export const bgError = signal<string>('');
let engine: BgEngine | null = null;
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
  try {
    const mod = await import('../../background/index');
    if (typeof mod.createBackground !== 'function') throw new Error('background module is a stub');
    engine = mod.createBackground(root, { workerUrl: 'assets/bg-worker.js' });
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
export function previewBg(cfg: BgConfig | null): void { engine?.preview(cfg); }
