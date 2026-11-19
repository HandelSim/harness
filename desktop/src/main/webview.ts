// <webview> lockdown (1.1): no preload, sandboxed, only serve/opencode origins, no popups, no permissions
// beyond clipboard write; Basic auth for opencode answered from main; cookies copied into the partition.
import { app, session, shell, type WebContents, type Session } from 'electron';
import { webviewSrcOk, externalUrlOk } from './security';
import { log } from './log';

export const WEB_PARTITION = 'persist:harness-web';

export interface WebAuth {
  origins(): string[];                                        // allowed guest origins (serve, opencode)
  basicAuth(origin: string): { username: string; password: string } | null;
}

let css = '';
export function setGuestCss(text: string): void { css = text.slice(0, 200000); }

export function hardenWebviews(host: WebContents, auth: WebAuth): void {
  host.on('will-attach-webview', (e, prefs, params) => {
    delete (prefs as { preload?: string }).preload;
    delete (prefs as { preloadURL?: string }).preloadURL;
    prefs.nodeIntegration = false; prefs.contextIsolation = true; prefs.sandbox = true;
    prefs.nodeIntegrationInSubFrames = false; prefs.webSecurity = true;
    prefs.partition = WEB_PARTITION;
    if (!webviewSrcOk(params.src, auth.origins())) {
      log('warn', `webview attach refused for ${String(params.src).slice(0, 200)}`);
      e.preventDefault();
    }
  });
  host.on('did-attach-webview', (_e, guest) => guardGuest(guest, auth));
}

function guardGuest(guest: WebContents, auth: WebAuth): void {
  const nav = (e: { preventDefault(): void }, url: string) => {
    if (webviewSrcOk(url, auth.origins())) return;
    e.preventDefault();
    if (externalUrlOk(url) && !process.env.HARNESS_DESKTOP_E2E) void shell.openExternal(url);
  };
  guest.on('will-navigate', nav);
  guest.on('will-frame-navigate', (e) => { if (e.isMainFrame) nav(e, e.url); });
  guest.on('will-redirect', nav);
  guest.setWindowOpenHandler(({ url }) => {
    if (externalUrlOk(url) && !process.env.HARNESS_DESKTOP_E2E) void shell.openExternal(url);
    return { action: 'deny' };
  });
  guest.on('dom-ready', () => { if (css) void guest.insertCSS(css).catch(() => {}); });
}

export function setupWebSession(auth: WebAuth): Session {
  const ses = session.fromPartition(WEB_PARTITION);
  ses.setPermissionRequestHandler((_wc, perm, cb) => cb(perm === 'clipboard-sanitized-write'));
  ses.setPermissionCheckHandler((_wc, perm) => perm === 'clipboard-sanitized-write');
  app.on('login', (e, _wc, details, authInfo, cb) => {
    if (authInfo.isProxy) return;
    let origin = '';
    try { origin = new URL(details.url).origin; } catch { return; }
    const cred = auth.basicAuth(origin);
    if (!cred) return;
    e.preventDefault();
    cb(cred.username, cred.password);
  });
  return ses;
}

export async function copyCookies(ses: Session, url: string, cookies: { name: string; value: string }[]): Promise<void> {
  for (const c of cookies) {
    await ses.cookies.set({ url, name: c.name, value: c.value, httpOnly: true, sameSite: 'strict' }).catch((e) =>
      log('warn', `cookie copy failed: ${e}`));
  }
}
