// node:http client to the loopback serve (D6). Sends no Origin/Referer/Sec-Fetch-*; keeps serve's
// session cookies in memory; GET may re-login once on 401 (the manager's single-flight hook); POST is never
// retried. Every request has a wall-clock deadline (connect included) and a response size cap (A2).
import * as http from 'node:http';
import type { ActionResult, ApiError, ApiErrorCode, ReadResult } from '../../shared/api';

export interface RawResponse { status: number; headers: http.IncomingHttpHeaders; body: Buffer }
export interface HttpOpts { method?: 'GET' | 'POST' | 'HEAD'; body?: string | Buffer; contentType?: string; timeoutMs?: number;
  /** Response body cap; past it the request is destroyed (default MAX_BODY). */
  maxBytes?: number }

/** Default response cap (A2): serve's biggest answers (vault files, exports) stay well under this. */
export const MAX_BODY = 64 * 1024 * 1024;

export class HttpError extends Error {
  constructor(public code: 'offline' | 'timeout' | 'unknown-result', message: string) { super(message); }
}

export class ServeClient {
  base: string;
  private cookies = new Map<string, string>();
  /** Called on 401 for GETs: should log in again (POST /login) and return true on success. */
  relogin: (() => Promise<boolean>) | null = null;
  /** Observers for headers (stale / pid / state id). */
  onHeaders: ((h: http.IncomingHttpHeaders) => void) | null = null;

  constructor(base: string) { this.base = base.replace(/\/+$/, ''); }

  cookieHeader(): string { return [...this.cookies].map(([k, v]) => `${k}=${v}`).join('; '); }
  cookieList(): { name: string; value: string }[] { return [...this.cookies].map(([name, value]) => ({ name, value })); }
  clearCookies(): void { this.cookies.clear(); }

  raw(path: string, o: HttpOpts = {}): Promise<RawResponse> {
    const url = new URL(this.base + path);
    const headers: Record<string, string> = { Accept: 'application/json', 'User-Agent': 'harness-desktop' };
    const ck = this.cookieHeader();
    if (ck) headers.Cookie = ck;
    let body: Buffer | undefined;
    if (o.body !== undefined) {
      body = Buffer.isBuffer(o.body) ? o.body : Buffer.from(o.body, 'utf8');
      headers['Content-Type'] = o.contentType || 'application/json';
      headers['Content-Length'] = String(body.length);
    }
    const ms = o.timeoutMs ?? 10000;
    const cap = o.maxBytes ?? MAX_BODY;
    return new Promise((resolve, reject) => {
      let sent = false;
      let done = false;
      // A2: one wall-clock deadline for the whole exchange (connect included); setTimeout on the request is
      // only a socket-idle timer and is not armed before the socket connects.
      const timer = setTimeout(() => {
        fail(new HttpError('timeout', `no answer in ${ms} ms`));
        req.destroy();
      }, ms);
      const fail = (e: Error) => { if (done) return; done = true; clearTimeout(timer); reject(e); };
      const req = http.request({ host: url.hostname, port: url.port, path: url.pathname + url.search, method: o.method || 'GET',
        headers, agent: false }, (res) => {
        const chunks: Buffer[] = [];
        let size = 0;
        res.on('data', (c: Buffer) => {
          size += c.length;
          if (size > cap) { fail(new HttpError(sent ? 'unknown-result' : 'offline', `response larger than ${cap} bytes`)); req.destroy(); return; }
          chunks.push(c);
        });
        res.on('end', () => {
          if (done) return;
          done = true; clearTimeout(timer);
          this.storeCookies(res.headers['set-cookie']);
          this.onHeaders?.(res.headers);
          resolve({ status: res.statusCode || 0, headers: res.headers, body: Buffer.concat(chunks) });
        });
        res.on('error', (e) => fail(new HttpError(sent ? 'unknown-result' : 'offline', String(e.message))));
        // A body cut short: 'aborted'/'close' without 'end' would otherwise leave the promise pending.
        res.on('close', () => fail(new HttpError(sent ? 'unknown-result' : 'offline', 'connection closed before the answer was complete')));
      });
      req.on('error', (e) => {
        if (e instanceof HttpError) fail(e);
        else fail(new HttpError(sent ? 'unknown-result' : 'offline', String(e.message)));
      });
      req.on('close', () => fail(new HttpError(sent ? 'unknown-result' : 'offline', 'connection closed')));
      req.on('finish', () => { sent = true; });
      req.end(body);
    });
  }

  private storeCookies(sc: string[] | undefined): void {
    for (const c of sc || []) {
      const [pair, ...attrs] = c.split(';');
      const i = pair.indexOf('=');
      if (i < 1) continue;
      const name = pair.slice(0, i).trim(); const value = pair.slice(i + 1).trim();
      const expired = attrs.some((a) => /^\s*max-age\s*=\s*0\s*$/i.test(a)) || value === '';
      if (expired) this.cookies.delete(name); else this.cookies.set(name, value);
    }
  }

  async get<T>(path: string, query?: Record<string, string | number | boolean>, sig?: string, timeoutMs = 10000): Promise<ReadResult<T>> {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query || {})) qs.set(k, String(v));
    if (sig) qs.set('sig', sig);
    const full = path + (qs.size ? `?${qs}` : '');
    let retried = false;
    for (;;) {
      let r: RawResponse;
      try { r = await this.raw(full, { timeoutMs }); } catch (e) { return toErr(e, 'offline'); }
      if (r.status === 401 && !retried && this.relogin) {
        retried = true;
        if (await this.relogin().catch(() => false)) continue;
      }
      return parseEnvelope<ReadResult<T>>(r);
    }
  }

  async post(path: string, body: Record<string, unknown> = {}, timeoutMs = 150000): Promise<ActionResult> {
    let r: RawResponse;
    try { r = await this.raw(path, { method: 'POST', body: JSON.stringify(body), timeoutMs }); } catch (e) {
      // A POST whose request was written may have run: never retried, reported as unknown-result.
      const err = e instanceof HttpError && e.code === 'timeout' ? new HttpError('unknown-result', e.message) : e;
      return toErr(err, 'offline');
    }
    return parseEnvelope<ActionResult>(r);
  }

  /** POST /login with the form field `code` (serve's own login). True when serve set a session cookie. */
  async login(code: string): Promise<boolean> { return (await this.loginStatus(code)) === 'ok'; }

  /** Like login(), but tells a refused code ('bad') from serve's sign-in limiter ('limited', HTTP 429: the
   * code was not even checked). Network failures throw. */
  async loginStatus(code: string): Promise<'ok' | 'bad' | 'limited'> {
    const before = this.cookies.size;
    const r = await this.raw('/login', { method: 'POST', body: new URLSearchParams({ code }).toString(),
      contentType: 'application/x-www-form-urlencoded', timeoutMs: 10000, maxBytes: 1024 * 1024 });
    if (r.status === 429) return 'limited';
    return (r.status === 302 || r.status === 303 || r.status === 200) && this.cookies.size > 0 && (this.cookies.size >= before) ? 'ok' : 'bad';
  }
}

export function apiError(status: number, code: ApiErrorCode, message: string): ApiError {
  return { ok: false, status, error: { code, message } };
}

function toErr(e: unknown, dflt: ApiErrorCode): ApiError {
  if (e instanceof HttpError) return apiError(0, e.code, e.message);
  return apiError(0, dflt, String((e as Error)?.message || e));
}

const STATUS_CODE: Record<number, ApiErrorCode> = { 400: 'bad-request', 401: 'auth', 403: 'origin', 404: 'not-found',
  405: 'method', 409: 'refused', 413: 'too-large', 415: 'content-type', 500: 'internal', 501: 'unavailable', 503: 'unavailable' };

export function parseEnvelope<T>(r: RawResponse): T {
  let j: unknown = null;
  try { j = JSON.parse(r.body.toString('utf8')); } catch { /* plain text (e.g. pub.py's 403) */ }
  if (r.status === 200 && j && typeof j === 'object' && (j as { ok?: unknown }).ok === true) return j as T;
  const o = (j && typeof j === 'object' ? j : {}) as { error?: { code?: string; message?: string } };
  const code = (o.error?.code as ApiErrorCode) || STATUS_CODE[r.status] || 'internal';
  const message = o.error?.message || (j ? '' : r.body.toString('utf8').slice(0, 500)) || `HTTP ${r.status}`;
  return apiError(r.status, code, message) as T;
}
