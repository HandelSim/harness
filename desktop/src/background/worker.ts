// Background worker entry (bundled to assets/bg-worker.js). Owns the OffscreenCanvas
// scenes; renders thumbnails. Every incoming message is validated.
import type { BgConfig, BgTheme } from '../shared/bg';
import { parseToWorker, type FromWorker, type ToWorker } from './protocol';
import { Scene } from './host';
import { EFFECTS } from './effects/registry';
import { DEFAULT_THEME } from './palette';
import { renderThumb } from './thumb';

const scope = self as unknown as DedicatedWorkerGlobalScope;
const post = (m: FromWorker) => scope.postMessage(m);
const raf = typeof scope.requestAnimationFrame === 'function'
  ? (cb: (t: number) => void) => scope.requestAnimationFrame(cb)
  : (cb: (t: number) => void) => setTimeout(() => cb(performance.now()), 16) as unknown as number;
const caf = typeof scope.cancelAnimationFrame === 'function' ? (h: number) => scope.cancelAnimationFrame(h) : (h: number) => clearTimeout(h);

let theme: BgTheme = DEFAULT_THEME;
let runState: { state: 'run' | 'dim' | 'pause'; reduced: boolean } = { state: 'run', reduced: false };
let burnMs = 0;
const scenes = new Map<number, Scene>();

function onMessage(m: ToWorker): void {
  switch (m.t) {
    case 'init': {
      const def = EFFECTS[m.config.id];
      if (!def) { post({ t: 'fail', id: m.id, reason: `no renderer for ${m.config.id}`, contextLost: false }); return; }
      const scene = new Scene(m.canvas, def, m.config, theme, m.allowGL, {
        raf, caf, now: () => performance.now(),
        onStats: (s, frames) => post({ t: 'stats', id: m.id, fps: s.fps, frameMs: s.frameMs, renderer: scene.renderer, tier: s.tier, downgraded: s.downgraded, reason: s.reason, frames }),
        onFail: (reason, contextLost) => post({ t: 'fail', id: m.id, reason, contextLost }),
      });
      try {
        scene.setState(runState.state, runState.reduced);
        scene.burn(burnMs);
        const r = scene.start(m.w, m.h);
        scenes.set(m.id, scene);
        post({ t: 'ready', id: m.id, renderer: r });
      } catch (e) {
        scene.dispose();
        post({ t: 'fail', id: m.id, reason: (e as Error).message || 'start failed', contextLost: false });
      }
      return;
    }
    case 'config': scenes.get(m.id)?.setConfig(m.config); return;
    case 'resize': scenes.get(m.id)?.resize(m.w, m.h); return;
    case 'drop': scenes.get(m.id)?.dispose(); scenes.delete(m.id); return;
    case 'theme': theme = m.theme; for (const s of scenes.values()) s.setTheme(theme); return;
    case 'state': runState = { state: m.state, reduced: m.reduced }; for (const s of scenes.values()) s.setState(m.state, m.reduced); return;
    case 'debug': burnMs = m.burnMs; for (const s of scenes.values()) s.burn(burnMs); return;
    case 'thumb': void thumb(m.req, m.config, m.w, m.h, m.theme); return;
  }
}

async function thumb(req: number, config: BgConfig, w: number, h: number, th: BgTheme): Promise<void> {
  try { post({ t: 'thumb', req, blob: await renderThumb(config, th, w, h), error: '' }); }
  catch (e) { post({ t: 'thumb', req, blob: null, error: (e as Error).message || 'thumbnail failed' }); }
}

scope.onmessage = (e: MessageEvent) => {
  const m = parseToWorker(e.data);
  if (m) onMessage(m);
};
