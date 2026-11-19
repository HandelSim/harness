// FILE: desktop/src/shared/bg.ts
export type BgGroup = 'static' | 'flow' | 'chaos' | 'automata' | 'fields' | 'system';
export interface BgParamSpec {
  key: string; label: string; type: 'range' | 'select' | 'toggle';
  min?: number; max?: number; step?: number; options?: { value: string; label: string }[];
  default: number | string | boolean;
}
export interface BgDescriptor {
  id: string; name: string; group: BgGroup; blurb: string;
  renderer: 'css' | 'worker2d' | 'webgl2';   // preferred; the engine may fall back
  dynamic: boolean; cost: 1 | 2 | 3;         // DR(e) cost class
  params: BgParamSpec[];
}
export interface BgConfig {
  id: string; seed: number; params: Record<string, number | string | boolean>;
  intensity: number;                          // 0..1 opacity/contrast of the field
  speed: number;                              // 0..2, 1 = designed speed
  fpsCap: 15 | 24 | 30 | 60;
}
export interface BgTheme { mode: 'dark' | 'light'; base: string; fg: string; accent: string; accent2: string }  // resolved CSS colours
export type BgRunState = 'run' | 'dim' | 'pause';
export interface BgStats {
  fps: number; frameMs: number; renderer: 'css' | 'worker2d' | 'webgl2' | 'main2d';
  tier: 0 | 1 | 2 | 3;                        // 0 = full, 3 = static fallback
  downgraded: boolean; reason: string;
}
export interface BgEngine {
  catalog(): BgDescriptor[];
  set(config: BgConfig, opts?: { fadeMs?: number }): Promise<void>;   // commit
  preview(config: BgConfig | null): void;                             // null reverts to the committed one
  setTheme(theme: BgTheme): void;
  setRunState(state: BgRunState): void;
  setReducedMotion(on: boolean): void;        // on: dynamic backgrounds render one settled frame and stop
  thumbnail(id: string, w: number, h: number, theme: BgTheme): Promise<string>;  // blob: URL, cached
  onStats(cb: (s: BgStats) => void): () => void;
  dispose(): void;
}
export interface BgCreateOpts { workerUrl: string }
// Implemented by desktop/src/background/index.ts:
//   export function createBackground(root: HTMLElement, opts: BgCreateOpts): BgEngine
//   export const DEFAULT_BG: BgConfig
