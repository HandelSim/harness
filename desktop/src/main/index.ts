// Main process entry: env, userData, scheme, window, IPC handlers, backend manager, quit policy.
import { app, BrowserWindow, dialog, Notification, powerMonitor, safeStorage, session, shell, ipcMain } from 'electron';
import { writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import type { AppBusy, AppInfo, AppState, BackendAction, PathInfo, Settings } from '../shared/ipc';
import type { Topic } from '../shared/api';
import { readEnv, defaultSettings, coerceSettings, mergeSettings } from './settings';
import { defaultState, coerceState, mergeState } from './state';
import { loadJson, saveJson } from './store';
import { initLog, log } from './log';
import { registerScheme, serveApp } from './protocol';
import { createWindow, crashReload, CRASH_PAGE } from './window';
import { handle, listen, emit, setEventTargets, recordEvents, sentLog, v, BadPayload, unavailable } from './ipc';
import { apiPathOk, bytesPathOk, externalUrlOk, APP_PREFIX } from './security';
import { BackendManager } from './backend/manager';
import { apiError } from './backend/http';
import { runHarness, parseConfigTsv, startJob, parseCheckUpdates, Jobs } from './cli';
import { pathInfo } from './pathinfo';
import { hardenWebviews, setupWebSession, copyCookies, setGuestCss, WEB_PARTITION } from './webview';
import { KeyVault } from './keys/vault';
import { PtyClient } from './pty/client';
import { Updater } from './updater';
import { buildMenu } from './menu';

const env = readEnv();
if (env.userData) app.setPath('userData', resolve(env.userData));
if (env.disableGpu) app.disableHardwareAcceleration();
registerScheme();
// Windows: the taskbar groups windows, pins and notifications by this id; it matches the NSIS shortcut's (appId).
if (process.platform === 'win32') app.setAppUserModelId('dev.harness.desktop');

// L6: a second instance exits at once and touches nothing (no store loads / corrupt-file moves, no ready work).
const secondary = !env.e2e && !app.requestSingleInstanceLock();
if (secondary) app.exit(0);

const userData = app.getPath('userData');
initLog(join(userData, 'logs'), env.logLevel);
const appPath = app.getAppPath();

const sFile = join(userData, 'settings.json');
const aFile = join(userData, 'state.json');
const sl = secondary ? { value: defaultSettings(), recovered: null } : loadJson(sFile, defaultSettings, coerceSettings);
let settings: Settings = sl.value;
if (sl.recovered) log('warn', `settings.json was unreadable; moved to ${sl.recovered}`);
const al = secondary ? { value: defaultState(), recovered: null } : loadJson(aFile, defaultState, coerceState);
let appState: AppState = al.value;
if (al.recovered) log('warn', `state.json was unreadable; moved to ${al.recovered}`);
let stateDirty = false;
const flushState = () => { if (stateDirty) { try { saveJson(aFile, appState); stateDirty = false; } catch (e) { log('error', `state save: ${e}`); } } };
setInterval(flushState, 2000).unref();

let win: BrowserWindow | null = null;
setEventTargets(() => (win && !win.isDestroyed() ? [win.webContents] : []));
recordEvents(env.e2e);

// L16: settings apply and broadcast at once; the fsync'd write is debounced (a slider drag is one write) and
// flushed on quit.
let settingsTimer: NodeJS.Timeout | null = null;
const flushSettings = () => {
  if (!settingsTimer) return;
  clearTimeout(settingsTimer); settingsTimer = null;
  try { saveJson(sFile, settings); } catch (e) { log('error', `settings save: ${e}`); }
};
const saveSettings = (patch: unknown) => {
  settings = mergeSettings(settings, patch);
  if (settingsTimer) clearTimeout(settingsTimer);
  settingsTimer = setTimeout(() => { settingsTimer = null; try { saveJson(sFile, settings); } catch (e) { log('error', `settings save: ${e}`); } }, 400);
  settingsTimer.unref?.();
  emit('settings:changed', settings);
  updater?.setAuto(settings.updates.auto);
  return settings;
};

const vault = new KeyVault({ file: join(userData, 'keys.json'), safe: safeStorage, root: () => backend.st.root, emit: (s) => emit('keys:changed', s) });

const backend = new BackendManager({
  env, appPath, packaged: app.isPackaged, home: homedir(), logDir: join(userData, 'logs'),
  settings: () => settings,
  patchSettings: (p) => { saveSettings(p); },
  emit: (s) => emit('backend:state', s),
  live: (topic, sig, data) => emit('live:update', { topic, sig, data }),
  health: (ok, failures, nextInMs) => emit('live:health', { ok, failures, nextInMs }),
  accessCode: () => vault.accessCode(),
  saveAccessCode: (c) => vault.saveAccessCode(c),
  lastProject: () => appState.recents.projects[0] || null,
  confirmHost: async () => {
    if (env.e2e) { e2eRecord.dialogs.push({ confirmHost: true }); return true; }
    const o = { type: 'warning' as const, buttons: ['Cancel', 'Allow host mode'], defaultId: 0, cancelId: 0, title: 'Host mode',
      message: 'Allow agents to run directly on this machine?',
      detail: 'Host sessions run without the container sandbox, with your user\'s full access to files and network.' };
    const r = win ? await dialog.showMessageBox(win, o) : await dialog.showMessageBox(o);
    return r.response === 1;
  },
});

const pty = new PtyClient({ hostPath: join(appPath, 'out/main/pty-host.cjs'), root: () => backend.st.root,
  hostConsent: () => settings.backend.hostConsent, emit });
const updater: Updater = new Updater({ env, packaged: app.isPackaged, version: app.getVersion(), emit: (s) => emit('update:state', s),
  auto: () => settings.updates.auto,
  beforeInstall: async () => { await shutdown(); app.releaseSingleInstanceLock(); },
  installFailed: () => { app.relaunch(); app.exit(0); } });

// e2e hooks (only with HARNESS_DESKTOP_E2E=1): recorded renderer-bound events, stubbed dialogs/notifications.
const e2eRecord: { notifications: unknown[]; dialogs: unknown[] } = { notifications: [], dialogs: [] };
if (env.e2e) Object.assign(globalThis, { __harnessE2E: { sentLog, record: e2eRecord, backend, pty, vault, updater } });

// ---------------- IPC ----------------
const obj = v.obj;
handle('app:info', v.none, (): AppInfo => ({
  version: app.getVersion(), platform: process.platform, arch: process.arch, packaged: app.isPackaged, e2e: env.e2e,
  userData, home: homedir(), electron: process.versions.electron, chrome: process.versions.chrome,
  keyStorage: vault.storageInfo(), reducedMotion: systemReducedMotion(), onBattery: powerMonitor.isOnBatteryPower(),
}));
handle('backend:get', v.none, () => backend.get());
handle('app:busy', v.none, (): AppBusy => ({ terminals: pty.liveCount(), queueRunning: backend.busy(),
  ownedServe: backend.st.owned, onQuit: settings.backend.onQuit, jobs: jobs.running() }));
handle('backend:action', (p): BackendAction => {
  const o = obj(p);
  const a = v.oneOf(o.action, 'action', ['start', 'restart', 'stop', 'retry', 'create-env', 'consent-host', 'create-code', 'set-root', 'login'] as const);
  if (a === 'set-root') return { action: a, root: v.str(o.root, 'root', 4096) };
  if (a === 'login') return { action: a, code: v.str(o.code, 'code', 512) };
  return { action: a };
}, (a) => backend.action(a), () => backend.get());

const offline = () => apiError(0, 'offline', 'backend offline');
handle('api:get', (p) => {
  const o = obj(p);
  if (!apiPathOk(o.path)) throw new BadPayload('path must start with /api/v1/');
  const q: Record<string, string | number | boolean> = {};
  if (o.query !== undefined && o.query !== null) for (const [k, x] of Object.entries(obj(o.query))) {
    if (['string', 'number', 'boolean'].includes(typeof x)) q[k] = x as string; else throw new BadPayload(`query ${k}`);
  }
  return { path: o.path, query: q, sig: v.optStr(o.sig, 'sig', 100) };
}, async (p) => (backend.client && backend.st.phase !== 'needs-login' ? backend.client.get(p.path, p.query, p.sig) : offline()),
(message, code) => apiError(400, code === 'internal' ? 'internal' : 'bad-request', message));
handle('api:post', (p) => {
  const o = obj(p);
  if (!apiPathOk(o.path)) throw new BadPayload('path must start with /api/v1/');
  return { path: o.path, body: o.body === undefined ? {} : obj(o.body) };
}, async (p) => {
  if (!backend.client || backend.st.phase !== 'up') return offline();
  const r = await backend.client.post(p.path, p.body);
  backend.poller.kick(150);
  return r;
}, (message, code) => apiError(400, code === 'internal' ? 'internal' : 'bad-request', message));
handle('api:bytes', (p) => {
  const o = obj(p);
  if (!bytesPathOk(o.path)) throw new BadPayload('path must start with /vault/file/');
  return o.path;
}, async (path) => {
  if (!backend.client) return { ok: false, mime: '', data: new Uint8Array() };
  const r = await backend.client.raw(path, { timeoutMs: 15000 }).catch(() => null);
  if (!r || r.status !== 200) return { ok: false, mime: '', data: new Uint8Array() };
  return { ok: true, mime: String(r.headers['content-type'] || 'application/octet-stream'), data: new Uint8Array(r.body) };
}, () => ({ ok: false, mime: '', data: new Uint8Array() }));
handle('live:set', (p) => {
  const t = obj(p).topics;
  if (!Array.isArray(t) || t.length > 64) throw new BadPayload('topics');
  return t.filter((x): x is Topic => typeof x === 'string' && /^[a-z-]+(:[A-Za-z0-9._-]+)?$/.test(x));
}, (topics, e) => { backend.poller.set(String(e.sender.id), topics); }, () => undefined);

// harness CLI
handle('harness:config-list', v.none, async () => {
  const root = backend.st.root;
  if (!root) return { ok: false, rows: [], message: 'no harness install' };
  const r = await runHarness(root, ['config', 'list', '--tsv'], { timeoutMs: 30000 });
  if (r.code !== 0) return { ok: false, rows: [], message: (r.stderr || r.stdout).trim().slice(-500) };
  return { ok: true, rows: parseConfigTsv(r.stdout), message: '' };
}, (m) => ({ ok: false, rows: [], message: m }));
const SECRET_RE = /KEY|SECRET|TOKEN|PASSWORD|COOKIE/;
handle('harness:config-set', (p) => {
  const o = obj(p);
  const key = v.str(o.key, 'key', 100);
  if (!/^[A-Z][A-Z0-9_]*$/.test(key)) throw new BadPayload('bad key');
  if (SECRET_RE.test(key)) throw new BadPayload('secret keys go through the key vault');
  return { key, value: v.str(o.value, 'value', 10000) };
}, async ({ key, value }) => {
  const root = backend.st.root;
  if (!root) return { ok: false, msg: 'no harness install' };
  const list = await runHarness(root, ['config', 'list', '--tsv'], { timeoutMs: 30000 });
  if (!parseConfigTsv(list.stdout).some((r) => r.key === key)) return { ok: false, msg: `${key} is not an editable setting` };
  const r = await runHarness(root, ['config', 'set', key, value], { timeoutMs: 30000 });
  return { ok: r.code === 0, msg: (r.code === 0 ? r.stdout : r.stderr || r.stdout).trim().slice(-500) || (r.code === 0 ? `${key} saved` : 'failed') };
}, (m) => ({ ok: false, msg: m }));
let harnessUpdate = { branch: '', local: '', available: null as boolean | null, text: '', checkedAt: null as number | null };
// M3: one running job per kind (a second request re-attaches to it), recent jobs kept with an output tail
const jobs = new Jobs({ start: (root, args, onChunk, onExit) => startJob(root, args, onChunk, onExit), emit,
  done: (job, all, code) => { if (job === 'check-updates') harnessUpdate = parseCheckUpdates(all, code, Date.now()); } });
handle('harness:job', (p) => v.oneOf(obj(p).job, 'job', ['doctor', 'check-updates', 'upgrade', 'serve-stop'] as const),
  (job) => jobs.run(job, backend.st.root), () => ({ id: '' }));
handle('harness:jobs', v.none, () => jobs.list(), () => []);
handle('harness:update-get', v.none, () => harnessUpdate);

// keys
vault.register(handle, v);

// settings + state
handle('settings:get', v.none, () => settings);
handle('settings:set', (p) => obj(obj(p).patch), (patch) => saveSettings(patch), () => settings);
handle('state:get', v.none, () => appState);
handle('state:set', (p) => obj(obj(p).patch), (patch) => {
  appState = mergeState(appState, patch); stateDirty = true;
  const pr = (patch as { recents?: { projects?: unknown } }).recents?.projects;
  if (pr) void 0;
}, () => undefined);

// pty
pty.register(handle, listen, v);

// web
const webAuth = {
  origins: () => [backend.client?.base, opencodeOrigin].filter((x): x is string => !!x).map((u) => new URL(u).origin),
  basicAuth: (origin: string) => (opencodeOrigin && origin === new URL(opencodeOrigin).origin && opencodePass
    ? { username: opencodeUser, password: opencodePass } : null),
};
let opencodeOrigin: string | null = null; let opencodePass = ''; let opencodeUser = 'opencode';
handle('web:url', (p) => {
  const o = obj(p);
  const target = v.oneOf(o.target, 'target', ['serve', 'opencode'] as const);
  const path = v.str(o.path, 'path', 4096);
  if (!path.startsWith('/') || path.startsWith('//')) throw new BadPayload('path must start with /');
  return { target, path };
}, async ({ target, path }) => {
  const c = backend.client;
  if (!c) return { ok: false, url: '', partition: WEB_PARTITION };
  const ses = setupWebSessionOnce();
  if (target === 'serve') {
    await copyCookies(ses, c.base, c.cookieList());
    return { ok: true, url: c.base + path, partition: WEB_PARTITION };
  }
  const r = await c.get<{ port: number; url: string; user: string; password: string }>('/api/v1/opencode');
  if (!r.ok || !r.data) return { ok: false, url: '', partition: WEB_PARTITION };
  const base = `http://127.0.0.1:${r.data.port}`;
  opencodeOrigin = base; opencodePass = r.data.password || ''; opencodeUser = r.data.user || 'opencode';
  return { ok: true, url: base + path, partition: WEB_PARTITION };
}, () => ({ ok: false, url: '', partition: WEB_PARTITION }));
handle('web:theme', (p) => v.str(obj(p).css, 'css', 200000), (css) => { setGuestCss(css); }, () => undefined);
let webSes: Electron.Session | null = null;
function setupWebSessionOnce(): Electron.Session { if (!webSes) webSes = setupWebSession(webAuth); return webSes; }

// updates
updater.register(handle, v);

// shell
handle('shell:open-external', (p) => { const u = obj(p).url; if (!externalUrlOk(u)) throw new BadPayload('url scheme not allowed'); return u; },
  async (url) => { if (env.e2e) { e2eRecord.dialogs.push({ openExternal: url }); return true; } await shell.openExternal(url); return true; }, () => false);
handle('shell:show-item', (p) => v.str(obj(p).path, 'path', 4096), (p) => { if (!env.e2e) shell.showItemInFolder(p); }, () => undefined);
handle('shell:pick-folder', (p) => v.optStr(obj(p ?? {}).defaultPath, 'defaultPath', 4096), async (defaultPath) => {
  if (env.e2e) { e2eRecord.dialogs.push({ pickFolder: defaultPath }); return (globalThis as { __pickFolder?: string }).__pickFolder ?? null; }
  if (!win) return null;
  const r = await dialog.showOpenDialog(win, { properties: ['openDirectory', 'createDirectory'], defaultPath });
  return r.canceled ? null : r.filePaths[0] ?? null;
}, () => null);
// M2 / F1 / F2: async (no sync fs or git on main), realpath'd, one git probe with root / branch / detached / commits.
handle('shell:path-info', (p) => { const o = obj(p); return { path: v.str(o.path, 'path', 4096), list: o.list === true }; },
  ({ path, list }): Promise<PathInfo> => pathInfo(path, list), () => ({ exists: false, dir: false, git: null }));
handle('shell:save-file', (p) => { const o = obj(p); return { name: v.str(o.name, 'name', 255), text: v.str(o.text, 'text', 50_000_000) }; }, async ({ name, text }) => {
  if (env.e2e) { const f = join(userData, 'saved-' + name.replace(/[^A-Za-z0-9._-]/g, '_')); writeFileSync(f, text); e2eRecord.dialogs.push({ saveFile: f }); return f; }
  if (!win) return null;
  const r = await dialog.showSaveDialog(win, { defaultPath: join(app.getPath('downloads'), name.replace(/[/\\]/g, '_')) });
  if (r.canceled || !r.filePath) return null;
  writeFileSync(r.filePath, text);
  return r.filePath;
}, () => null);

// window
handle('window:action', (p) => {
  const o = obj(p);
  return { action: v.oneOf(o.action, 'action', ['minimize', 'maximize', 'close', 'fullscreen', 'zoom'] as const),
    value: typeof o.value === 'number' && Number.isFinite(o.value) ? o.value : undefined };
}, ({ action, value }) => {
  if (!win) return;
  if (action === 'minimize') win.minimize();
  else if (action === 'maximize') { if (win.isMaximized()) win.unmaximize(); else win.maximize(); }
  else if (action === 'fullscreen') win.setFullScreen(!win.isFullScreen());
  else if (action === 'zoom') win.webContents.setZoomFactor(Math.min(2, Math.max(0.5, value ?? 1)));
  else if (action === 'close') { closeAllowed = true; win.close(); }
}, () => undefined);

// notifications
handle('notify:show', (p) => {
  const o = obj(p);
  return { id: v.str(o.id, 'id', 200), title: v.str(o.title, 'title', 300), body: v.str(o.body, 'body', 2000), route: v.str(o.route, 'route', 500) };
}, (n) => {
  if (env.e2e || !Notification.isSupported()) { e2eRecord.notifications.push(n); e2eRecord.notifications.splice(0, e2eRecord.notifications.length - 200); return; }
  // N2: hold a reference until click/close (a collected Notification loses its click handler); at most 20
  const note = new Notification({ title: n.title, body: n.body });
  liveNotes.add(note);
  if (liveNotes.size > 20) liveNotes.delete(liveNotes.values().next().value!);
  note.on('click', () => { liveNotes.delete(note); win?.show(); win?.focus(); emit('notify:click', { route: n.route }); });
  note.on('close', () => { liveNotes.delete(note); });
  note.show();
}, () => undefined);
const liveNotes = new Set<Notification>();

listen('log:write', (p) => { const o = obj(p); return { level: v.oneOf(o.level, 'level', ['info', 'warn', 'error'] as const), msg: v.str(o.msg, 'msg', 20000) }; },
  ({ level, msg }) => log(level, `renderer: ${msg}`));
ipcMain.handle('ping', () => Date.now());   // e2e responsiveness probe (no payload, no side effects)

/** Main has no reduced-motion API; the renderer's matchMedia('(prefers-reduced-motion: reduce)') is the real signal
 * (Chromium reads it from the OS). This is the e2e/env override only. */
function systemReducedMotion(): boolean {
  return process.env.HARNESS_DESKTOP_REDUCED_MOTION === '1';
}

// ---------------- lifecycle ----------------
let closeAllowed = false;
let quitting = false;

/** Quit work shared by before-quit and restart-to-update: state flushed, serve per policy, ptys killed (<= 8 s). */
let shutdownP: Promise<void> | null = null;
function shutdown(): Promise<void> {
  if (shutdownP) return shutdownP;
  quitting = true;
  flushState(); flushSettings();
  shutdownP = Promise.race([Promise.all([backend.onQuit(), pty.shutdown(), jobs.stopAll()]).then(() => undefined),
    new Promise<void>((r) => setTimeout(r, 8000))]).catch((e) => log('error', `shutdown: ${e}`));
  return shutdownP;
}

app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
app.on('web-contents-created', (_e, wc) => {
  wc.on('will-attach-webview', () => { /* handled per host in hardenWebviews */ });
  if (wc.getType() === 'webview') return;
});

app.whenReady().then(async () => {
  if (secondary) return;
  // L7: permissions denied on the default session except clipboard writes (terminal / copy buttons); the web
  // partition's handlers are installed now, not on the first web:url
  session.defaultSession.setPermissionRequestHandler((_wc, perm, cb) => cb(perm === 'clipboard-sanitized-write'));
  session.defaultSession.setPermissionCheckHandler((_wc, perm) => perm === 'clipboard-sanitized-write');
  setupWebSessionOnce();
  serveApp(join(appPath, 'out/renderer'));
  buildMenu((command) => emit('menu:command', { command }));
  win = createWindow(appPath, appState.window, (w) => { appState = { ...appState, window: w }; stateDirty = true; });
  hardenWebviews(win.webContents, webAuth);
  win.on('focus', () => backend.poller.setVisibility('focused'));
  win.on('blur', () => backend.poller.setVisibility(win?.isVisible() && !win.isMinimized() ? 'blurred' : 'hidden'));
  win.on('minimize', () => backend.poller.setVisibility('hidden'));
  win.on('hide', () => backend.poller.setVisibility('hidden'));
  win.on('restore', () => backend.poller.setVisibility(win?.isFocused() ? 'focused' : 'blurred'));
  win.on('close', (e) => {
    if (closeAllowed || quitting || env.e2e && !rendererReady) return;
    e.preventDefault();
    emit('app:will-quit', {});
    setTimeout(() => { closeAllowed = true; win?.close(); }, 1500).unref();
  });
  win.on('closed', () => { win = null; });
  // M5: a crashed renderer is reloaded (ptys survive and re-attach from the replay ring), capped at 3 a minute
  const crashes: number[] = [];
  win.webContents.on('render-process-gone', (_e, d) => {
    log('error', `renderer gone: ${d.reason}`);
    if (quitting || !win || win.isDestroyed()) return;
    const act = crashReload(crashes, d.reason, Date.now());
    if (act === 'reload') { rendererReady = false; void win.loadURL(`${APP_PREFIX}index.html`).catch((e) => log('error', `reload: ${e}`)); }
    else if (act === 'give-up') void win.loadURL(CRASH_PAGE).catch(() => undefined);
  });
  win.webContents.on('did-finish-load', () => { rendererReady = true; });
  const pm = () => emit('power:state', { onBattery: powerMonitor.isOnBatteryPower(), idle: powerMonitor.getSystemIdleState(300) !== 'active' });
  powerMonitor.on('on-battery', pm); powerMonitor.on('on-ac', pm); powerMonitor.on('lock-screen', pm); powerMonitor.on('unlock-screen', pm);
  // L12: after sleep / unlock poll now instead of waiting out the current backoff (up to 15 s)
  const wake = () => { pm(); backend.poller.kick(0); };
  powerMonitor.on('resume', wake); powerMonitor.on('unlock-screen', () => backend.poller.kick(0));
  await win.loadURL(`${APP_PREFIX}index.html`).catch((e) => log('error', `load: ${e}`));
  // the packaged smoke never starts or attaches a backend (it must not find and start a real install's serve)
  if (process.argv.includes('--smoke-test')) { void smokeTest(); return; }
  void backend.start();
  updater.init();
}).catch((e) => log('error', `startup: ${e?.stack || e}`));
let rendererReady = false;

app.on('window-all-closed', () => { app.quit(); });
app.on('before-quit', (e) => {
  if (quitting) return;                       // second pass, or restart-to-update already ran shutdown()
  e.preventDefault();
  void shutdown().finally(() => app.exit(0));
});
process.on('uncaughtException', (e) => log('error', `uncaught: ${e?.stack || e}`));
process.on('unhandledRejection', (e) => log('error', `unhandled: ${(e as Error)?.stack || e}`));

/** Packaged smoke (CI, PLAN 8): the window loaded app://, the preload bridge exists and a PTY echoes. Exit 0 or 1. */
async function smokeTest(): Promise<void> {
  const fail = (why: string) => { process.stderr.write(`smoke: FAIL ${why}\n`); app.exit(1); };
  setTimeout(() => fail('timeout'), 45000).unref();
  const w = BrowserWindow.getAllWindows()[0];
  if (!w || !w.webContents.getURL().startsWith(APP_PREFIX)) return fail('window did not load app://');
  const bridge = await w.webContents.executeJavaScript('typeof window.harness').catch(() => 'error');
  if (bridge !== 'object') return fail(`bridge is ${bridge}`);
  const r = await pty.spawn({ kind: 'plain', cwd: homedir() }, 80, 24);
  if (!r.ok) return fail(`pty: ${r.error.message}`);
  const out = await pty.smokeEcho(r.id, 'echo smoke-$((6*7))\r', 'smoke-42', 10000);
  if (!out) return fail('pty did not echo');
  process.stdout.write('smoke: OK\n');
  app.exit(0);
}
