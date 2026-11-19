// Pure allowlists (unit-tested in security.test.ts). No electron import here.
import { normalize, sep, join } from 'node:path';

export const APP_ORIGIN = 'app://harness';
export const APP_PREFIX = 'app://harness/';

export const CSP = "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self' 'unsafe-inline'; " +
  "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; " +
  "form-action 'none'; frame-ancestors 'none'";

/** Only app://harness/ frames may call IPC. */
export function senderOk(url: string | undefined | null): boolean {
  return typeof url === 'string' && url.startsWith(APP_PREFIX);
}

/** api:get / api:post path rule: /api/v1/ only, no traversal, no scheme/host smuggling. */
export function apiPathOk(path: unknown): path is string {
  if (typeof path !== 'string' || path.length > 2048) return false;
  if (!/^\/api\/v1\/[A-Za-z0-9._~:@!$&'()*+,;=%\/-]*$/.test(path)) return false;
  return !hasTraversal(path);
}

/** api:bytes rule: /vault/file/<rel> only. */
export function bytesPathOk(path: unknown): path is string {
  if (typeof path !== 'string' || path.length > 2048) return false;
  if (!/^\/vault\/file\/[A-Za-z0-9._~:@!$&'()*+,;=%\/-]+$/.test(path)) return false;
  return !hasTraversal(path);
}

function hasTraversal(path: string): boolean {
  let dec = path;
  try { dec = decodeURIComponent(path); } catch { return true; }
  return dec.split('/').some((s) => s === '..' || s === '.') || dec.includes('\\') || dec.includes('\0') || dec.includes('//');
}

/** Backend URL rule: http://127.0.0.1:<port> or http://localhost:<port>, nothing else. */
export function backendUrlOk(url: unknown): url is string {
  if (typeof url !== 'string') return false;
  let u: URL;
  try { u = new URL(url); } catch { return false; }
  if (u.protocol !== 'http:') return false;
  if (u.hostname !== '127.0.0.1' && u.hostname !== 'localhost') return false;
  if (!u.port) return false;
  if (u.username || u.password) return false;
  return (u.pathname === '/' || u.pathname === '') && !u.search && !u.hash;
}

/** shell:open-external: http, https, mailto only. */
export function externalUrlOk(url: unknown): url is string {
  if (typeof url !== 'string' || url.length > 4096) return false;
  try {
    const u = new URL(url);
    return u.protocol === 'http:' || u.protocol === 'https:' || u.protocol === 'mailto:';
  } catch { return false; }
}

/** Map an app:// URL onto a file below `base`; null when outside or malformed. */
export function appUrlToFile(url: string, base: string): string | null {
  let u: URL;
  try { u = new URL(url); } catch { return null; }
  if (u.protocol !== 'app:' || u.host !== 'harness') return null;
  let p: string;
  try { p = decodeURIComponent(u.pathname); } catch { return null; }
  if (p.includes('\0')) return null;
  if (p === '/' || p === '') p = '/index.html';
  const file = normalize(join(base, p));
  const b = normalize(base.endsWith(sep) ? base : base + sep);
  return file.startsWith(b) ? file : null;
}

/** Webview src rule: origin must be one of the allowed origins (serve, opencode). */
export function webviewSrcOk(src: unknown, allowed: string[]): boolean {
  if (typeof src !== 'string') return false;
  try { return allowed.includes(new URL(src).origin); } catch { return false; }
}

export const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.map': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.ico': 'image/x-icon',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.wasm': 'application/wasm', '.txt': 'text/plain; charset=utf-8',
};
