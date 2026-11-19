// App self-update (D10): electron-updater, generic provider at the rolling desktop-latest release.
// HARNESS_DESKTOP_UPDATE_URL overrides the feed and (with forceDevUpdateConfig) makes it work unpackaged.
import type { AppUpdateState } from '../shared/ipc';
import { DEFAULT_UPDATE_URL, RELEASE_PAGE, type Env } from './settings';
import { log } from './log';
import type { handle as Handle, v as V } from './ipc';

type AU = import('electron-updater').AppUpdater;

/** Why self-update cannot work here, or '' when it can. Pure; unit-tested. */
export function unsupportedReason(o: { packaged: boolean; feedFromEnv: boolean; noUpdate: boolean; platform: string; appImage: boolean }): string {
  if (o.noUpdate) return 'updates are turned off (HARNESS_DESKTOP_NO_UPDATE=1)';
  if (o.feedFromEnv) return '';
  if (!o.packaged) return 'this is a development build: run from source, update with git';
  if (o.platform === 'linux' && !o.appImage) return 'on Linux the app updates itself only when installed as an AppImage';
  return '';
}

export class Updater {
  st: AppUpdateState;
  private au: AU | null = null;
  private timer: NodeJS.Timeout | null = null;
  private first: NodeJS.Timeout | null = null;
  private auto = true;
  private installing = false;
  constructor(private d: { env: Env; packaged: boolean; version: string; emit: (s: AppUpdateState) => void;
    /** settings.updates.auto (M10): background checks + auto-download on, or neither. */
    auto?: () => boolean;
    /** H2: before quitAndInstall: stop serve per policy, kill ptys, flush state, release the single-instance lock. */
    beforeInstall?: () => Promise<void>;
    /** quitAndInstall refused after beforeInstall already ran (terminals are gone): restart the current version. */
    installFailed?: () => void;
    /** tests: the electron-updater instance. */
    au?: AU }) {
    const reason = unsupportedReason({ packaged: d.packaged, feedFromEnv: !!d.env.updateUrl, noUpdate: d.env.noUpdate,
      platform: process.platform, appImage: !!process.env.APPIMAGE });
    this.st = { phase: reason ? 'unsupported' : 'idle', current: d.version, next: null, notes: '', percent: 0, reason,
      checkedAt: null, releasePage: RELEASE_PAGE };
  }

  private set(p: Partial<AppUpdateState>): AppUpdateState { this.st = { ...this.st, ...p }; this.d.emit(this.st); return this.st; }

  private load(): AU | null {
    if (this.au || this.st.phase === 'unsupported') return this.au;
    try {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const au = this.d.au ?? (require('electron-updater') as typeof import('electron-updater')).autoUpdater;
      au.autoDownload = this.auto;
      au.autoInstallOnAppQuit = true;
      au.logger = { info: (m: unknown) => log('info', `updater: ${m}`), warn: (m: unknown) => log('warn', `updater: ${m}`),
        error: (m: unknown) => log('error', `updater: ${m}`), debug: () => {} };
      if (this.d.env.updateUrl) au.forceDevUpdateConfig = true;
      au.setFeedURL({ provider: 'generic', url: this.d.env.updateUrl || DEFAULT_UPDATE_URL });
      au.on('checking-for-update', () => this.set({ phase: 'checking' }));
      au.on('update-available', (i) => this.set({ phase: 'available', next: i.version, notes: notesText(i.releaseNotes), checkedAt: Date.now() }));
      au.on('update-not-available', () => this.set({ phase: 'idle', next: null, checkedAt: Date.now() }));
      au.on('download-progress', (p) => this.set({ phase: 'downloading', percent: Math.round(p.percent) }));
      au.on('update-downloaded', (i) => this.set({ phase: 'ready', next: i.version, percent: 100 }));
      au.on('error', (e) => this.set({ phase: 'error', reason: String(e?.message || e).slice(0, 500) }));
      this.au = au;
    } catch (e) {
      this.set({ phase: 'unsupported', reason: `updater failed to load: ${e}` });
    }
    return this.au;
  }

  init(): void {
    this.auto = this.d.auto ? this.d.auto() : true;
    if (this.st.phase === 'unsupported' || this.d.env.e2e) return;
    this.schedule();
  }

  /** settings:changed: updates.auto turns the background checks and auto-download on or off. */
  setAuto(on: boolean): void {
    if (on === this.auto) return;
    this.auto = on;
    if (this.au) this.au.autoDownload = on;
    if (this.st.phase === 'unsupported' || this.d.env.e2e) return;
    this.schedule();
  }

  scheduled(): boolean { return !!this.timer; }

  private schedule(): void {
    if (this.first) { clearTimeout(this.first); this.first = null; }
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    if (!this.auto) return;
    const run = () => { void this.check(); };
    this.first = setTimeout(run, 30000); this.first.unref();
    this.timer = setInterval(run, 6 * 3600 * 1000);
    this.timer.unref();
  }

  async check(): Promise<AppUpdateState> {
    const au = this.load();
    if (!au) return this.st;
    try { this.set({ phase: 'checking', reason: '' }); await au.checkForUpdates(); }
    catch (e) { this.set({ phase: 'error', reason: String((e as Error)?.message || e).slice(0, 500) }); }
    return this.st;
  }

  async download(): Promise<AppUpdateState> {
    const au = this.load();
    if (!au || this.st.phase !== 'available') return this.st;
    try { this.set({ phase: 'downloading', percent: 0 }); await au.downloadUpdate(); }
    catch (e) { this.set({ phase: 'error', reason: String((e as Error)?.message || e).slice(0, 500) }); }
    return this.st;
  }

  /** Restart to update (H2). AppImage's doInstall spawns the new instance at once, so everything that blocks a
   * clean handover (the single-instance lock, serve, ptys) is released first; the app then quits normally. */
  async install(): Promise<void> {
    const au = this.au;
    if (!au || this.st.phase !== 'ready' || this.installing) return;
    this.installing = true;
    try { await this.d.beforeInstall?.(); } catch (e) { log('error', `before install: ${e}`); }
    au.quitAndInstall(false, true);
    // electron-updater reports a refused install through 'error' (synchronously) and does not quit
    // (phase is set by the 'error' listener during the call)
    if ((this.st as AppUpdateState).phase === 'error') { this.installing = false; this.d.installFailed?.(); }
  }

  register(handle: typeof Handle, v: typeof V): void {
    handle('update:get', v.none, () => this.st, () => this.st);
    handle('update:check', v.none, () => this.check(), () => this.st);
    handle('update:download', v.none, () => this.download(), () => this.st);
    handle('update:install', v.none, () => this.install(), () => undefined);
  }
}

function notesText(n: unknown): string {
  if (typeof n === 'string') return n.replace(/<[^>]+>/g, '').slice(0, 20000);
  if (Array.isArray(n)) return n.map((x) => (x && typeof x === 'object' && 'note' in x ? String((x as { note: unknown }).note) : '')).join('\n').replace(/<[^>]+>/g, '').slice(0, 20000);
  return '';
}
