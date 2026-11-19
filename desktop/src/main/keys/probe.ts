// Key probes (W13). Active key: `harness unlock` output; inactive key: main's own 1-token probe, classified
// like the CLI's _probe_upstream_auth. A network failure is 'unknown', never 'bad'.
import * as http from 'node:http';
import * as https from 'node:https';
import type { KeyProbe } from '../../shared/ipc';
import { extractUnlockUrl } from '../backend/classify';

/** The CLI's _api_base: trim trailing /, /v1/chat/completions or /chat/completions, then /v1. */
export function apiBase(url: string): string {
  let b = url.replace(/\/+$/, '');
  if (b.endsWith('/v1/chat/completions')) b = b.slice(0, -'/v1/chat/completions'.length);
  else if (b.endsWith('/chat/completions')) b = b.slice(0, -'/chat/completions'.length);
  b = b.replace(/\/+$/, '');
  if (b.endsWith('/v1')) b = b.slice(0, -3);
  return b.replace(/\/+$/, '');
}

/** Classify an upstream answer to the probe (status + body), the table of INV 5. */
export function classifyProbe(status: number, body: string, now: number): KeyProbe {
  if (status >= 200 && status < 300) return { state: 'ok', at: now, message: 'key works' };
  let j: { error?: { type?: string; message?: string; unlock_url?: string; metadata?: { unlock_url?: string } }; unlock_url?: string } | null = null;
  try { j = JSON.parse(body); } catch { /* not JSON */ }
  const unlock = j?.error?.unlock_url || j?.error?.metadata?.unlock_url || j?.unlock_url || /"unlock_url"\s*:\s*"([^"]*)"/.exec(body)?.[1];
  const type = (j?.error?.type || /"type"\s*:\s*"([^"]*)"/.exec(body)?.[1] || '').toLowerCase();
  const message = (j?.error?.message || /"message"\s*:\s*"([^"]*)"/.exec(body)?.[1] || '').slice(0, 500);
  if (status === 401 || status === 403) {
    if (unlock) return { state: 'locked', at: now, message: message || 'key is locked', unlockUrl: unlock };
    if (type.startsWith('invalid_request')) return { state: 'ok', at: now, message: 'key accepted (the probe request itself was refused)' };
    return { state: 'rejected', at: now, message: message || `HTTP ${status}` };
  }
  if (message.includes('access to this model')) return { state: 'ok', at: now, message: `key works; model refused: ${message}` };
  return { state: 'unknown', at: now, message: message || `HTTP ${status}` };
}

/** `harness unlock` result: exit 0 + "upstream API key is OK" -> ok; exit 1 with the banner -> locked/rejected. */
export function classifyUnlock(code: number, out: string, now: number): KeyProbe {
  if (code === 0 && /upstream API key is OK/i.test(out)) return { state: 'ok', at: now, message: 'key works' };
  if (/Upstream API key is locked/i.test(out)) {
    const u = extractUnlockUrl(out);
    return { state: 'locked', at: now, message: 'key is locked', ...(u ? { unlockUrl: u } : {}) };
  }
  if (/Upstream rejected the API key/i.test(out)) {
    const m = /upstream message:\s*(.+)/.exec(out)?.[1]?.trim();
    return { state: 'rejected', at: now, message: m || 'key was rejected' };
  }
  if (code === 0) return { state: 'ok', at: now, message: out.trim().split('\n').pop()?.slice(0, 300) || 'ok' };
  return { state: 'unknown', at: now, message: out.trim().split('\n').slice(-2).join(' ').slice(0, 300) || `exit ${code}` };
}

/** POST <base>/v1/chat/completions with max_tokens 1 (30 s), like _probe_upstream_auth. */
export function probeDirect(baseUrl: string, key: string, model: string, timeoutMs = 30000): Promise<KeyProbe> {
  return new Promise((resolve) => {
    const now = Date.now();
    let u: URL;
    try { u = new URL(apiBase(baseUrl) + '/v1/chat/completions'); } catch { resolve({ state: 'unknown', at: now, message: 'bad upstream URL' }); return; }
    const body = JSON.stringify({ model: model || 'probe', max_tokens: 1, messages: [{ role: 'user', content: 'ping' }] });
    const mod = u.protocol === 'https:' ? https : http;
    const req = mod.request(u, { method: 'POST', headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(body) } }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c: Buffer) => { if (chunks.length < 64) chunks.push(c); });
      res.on('end', () => resolve(classifyProbe(res.statusCode || 0, Buffer.concat(chunks).toString('utf8'), Date.now())));
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error('timeout')));
    req.on('error', (e) => resolve({ state: 'unknown', at: Date.now(), message: `could not reach upstream: ${e.message}` }));
    req.end(body);
  });
}
