// No-GPU policy. probeGpu() asks WebGL for the real renderer string once (main thread); a software
// rasteriser (SwiftShader, llvmpipe, softpipe, WARP) or no WebGL at all means "no usable GPU". On such
// hosts low power defaults on: heavy effects become their CSS variant, the rest run at lower resolution
// and a capped frame rate (lowPowerPlan). Pure except probeGpu().
import type { BgDescriptor } from '../shared/bg';

export interface GpuInfo { renderer: string; software: boolean }
export type LowPowerMode = 'auto' | 'on' | 'off';
export interface LowPowerPlan { mode: 'full' | 'reduced' | 'static'; fpsCap: number; scaleMul: number }

export const LOW_POWER_FPS = 20;
export const LOW_POWER_SCALE = 0.5;
export const LOW_POWER_REASON = 'low power (software GL)';
/** Ids that stay too heavy on a software rasteriser even at half resolution and 20 fps (L4 measurements:
 *  main-thread rAF p95 of 100-300 ms for tracers and 83-217 ms for grayscott on SwiftShader). */
const STATIC_ON_LOW_POWER = new Set(['tracers', 'grayscott']);
const SOFTWARE_RE = /swiftshader|llvmpipe|softpipe|software|basic render|lavapipe/i;

/** Classify a WebGL renderer string. `hasContext` false (no WebGL at all) also counts as software. */
export function classifyRenderer(renderer: string | null | undefined, hasContext = true): GpuInfo {
  const r = String(renderer ?? '').slice(0, 256);
  return { renderer: hasContext ? r : 'none', software: !hasContext || SOFTWARE_RE.test(r) };
}

export function isLowPowerMode(v: unknown): v is LowPowerMode { return v === 'auto' || v === 'on' || v === 'off'; }
export function lowPowerActive(mode: LowPowerMode, gpu: GpuInfo): boolean { return mode === 'on' || (mode === 'auto' && gpu.software); }

/** What low power does to one catalog entry. Static (CSS) ids are untouched. */
export function lowPowerPlan(desc: Pick<BgDescriptor, 'id' | 'dynamic'>): LowPowerPlan {
  if (!desc.dynamic) return { mode: 'full', fpsCap: 60, scaleMul: 1 };
  if (STATIC_ON_LOW_POWER.has(desc.id)) return { mode: 'static', fpsCap: 0, scaleMul: 1 };
  return { mode: 'reduced', fpsCap: LOW_POWER_FPS, scaleMul: LOW_POWER_SCALE };
}

let cached: GpuInfo | null = null;
/** Main thread, once per page: a 1x1 WebGL2 (else WebGL1) context, read the unmasked renderer, drop the context. */
export function probeGpu(): GpuInfo {
  if (cached) return cached;
  let gl: WebGL2RenderingContext | WebGLRenderingContext | null = null;
  try {
    const c = typeof document !== 'undefined' ? document.createElement('canvas') : typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(1, 1) : null;
    if (c) { c.width = 1; c.height = 1; }
    const opts = { failIfMajorPerformanceCaveat: false, antialias: false, depth: false, stencil: false, powerPreference: 'low-power' } as const;
    gl = (c?.getContext('webgl2', opts) ?? c?.getContext('webgl', opts) ?? null) as WebGL2RenderingContext | WebGLRenderingContext | null;
    if (!gl) return (cached = classifyRenderer('', false));
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    const unmasked = ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) ?? '') : '';
    const plain = String(gl.getParameter(gl.RENDERER) ?? '');
    cached = classifyRenderer(unmasked && unmasked !== plain ? `${unmasked}` : plain || unmasked);
  } catch (e) {
    cached = { renderer: `probe failed: ${(e as Error)?.message ?? e}`.slice(0, 256), software: true };
  } finally {
    try { gl?.getExtension('WEBGL_lose_context')?.loseContext(); } catch { /* already gone */ }
  }
  return cached;
}
