// The one handle on the preload bridge (window.harness, PLAN 5.3). Outside Electron (or before L2's preload
// exposes a member) every call answers the documented 'unavailable' error, so the shell still renders.
import type { Bridge, BridgeEvents } from '../../shared/ipc';
import { API_PREFIX } from '../../shared/api';

const unavailable = { ok: false as const, status: 0, error: { code: 'unavailable' as const, message: 'desktop bridge not available' } };

function stub(): Bridge {
  const none = async () => unavailable as never;
  const handler: ProxyHandler<object> = {
    get: (_t, prop) => {
      if (prop === 'on') return () => () => {};
      if (prop === 'log') return () => {};
      return new Proxy(() => {}, { get: () => none, apply: () => unavailable });
    },
  };
  return new Proxy({}, handler) as Bridge;
}

export const bridge: Bridge = (typeof window !== 'undefined' && (window as { harness?: Bridge }).harness) || stub();
export const hasBridge = typeof window !== 'undefined' && !!(window as { harness?: Bridge }).harness;

export function on<K extends keyof BridgeEvents>(event: K, cb: (p: BridgeEvents[K]) => void): () => void {
  try { return bridge.on(event, cb); } catch { return () => {}; }
}

/** Absolute API path from a relative one ('/queue' -> '/api/v1/queue'). */
export const api = (p: string) => (p.startsWith(API_PREFIX) ? p : API_PREFIX + (p.startsWith('/') ? p : '/' + p));
export const enc = encodeURIComponent;
