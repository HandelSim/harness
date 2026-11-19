// app://harness/ scheme: serves out/renderer/** only, with the CSP header on every response.
import { protocol } from 'electron';
import { readFile } from 'node:fs/promises';
import { extname } from 'node:path';
import { appUrlToFile, CSP, MIME } from './security';

export function registerScheme(): void {
  protocol.registerSchemesAsPrivileged([{ scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true,
    stream: true, codeCache: true } }]);
}

export function serveApp(base: string): void {
  protocol.handle('app', async (req) => {
    const file = appUrlToFile(req.url, base);
    const headers: Record<string, string> = { 'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff',
      'Cache-Control': 'no-cache' };
    if (!file) return new Response('forbidden', { status: 403, headers });
    try {
      const body = await readFile(file);
      headers['Content-Type'] = MIME[extname(file).toLowerCase()] || 'application/octet-stream';
      return new Response(body, { status: 200, headers });
    } catch {
      return new Response('not found', { status: 404, headers });
    }
  });
}
