// e2e fixture (PLAN 7.5): `test` extended with mock, app, win and launch(opts). Electron runs under xvfb (scripts/e2e.mjs).
import { test as base, expect, _electron, type ElectronApplication, type Page } from '@playwright/test';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
// @ts-expect-error plain ESM helper without types
import { startMock as startMockRaw } from './mock-serve/server.mjs';

export type ScenarioName = 'healthy' | 'empty' | 'codes-on' | 'old-serve' | 'incompatible' | 'viewer' | 'stale'
  | 'queue-running' | 'needs-answers' | 'plan-changed' | 'refuse-all' | 'slow-actions' | 'flaky';
export interface MockServe {
  url: string;
  scenario(name: ScenarioName): Promise<void>;
  setTopic(topic: string, data: unknown): Promise<void>;
  onPost(path: string, reply: { status?: number; body?: unknown; delayMs?: number; drop?: boolean }): Promise<void>;
  fail(mode: 'none' | 'drop' | '500' | 'hang', count?: number): Promise<void>;
  requests(filter?: string): Promise<{ method: string; path: string; headers: Record<string, string>; body: unknown }[]>;
  stop(): Promise<void>; start(): Promise<void>;
}
export interface LaunchOpts { userData?: string; fakeHarness?: { env?: 'ok' | 'missing' | 'empty-key'; unlock?: 'ok' | 'locked' | 'rejected';
  serve?: 'ok' | 'fails' }; attach?: boolean; args?: string[];
  /** L2 extension (optional): extra environment for the app, e.g. HARNESS_DESKTOP_UPDATE_URL. */
  env?: Record<string, string> }
/** The mock plus extras the platform specs use (access code, update feed). */
export interface MockExtra extends MockServe { port: number; setFeed(f: { version: string }): Promise<void>; accessCode(): Promise<string> }

export const DESKTOP = resolve(__dirname, '..');
export const ELECTRON = join(DESKTOP, 'node_modules', 'electron', 'dist', process.platform === 'darwin' ? 'Electron.app/Contents/MacOS/Electron' : process.platform === 'win32' ? 'electron.exe' : 'electron');
/** Settled in M0 (docs/progress/L2.md): software GL through ANGLE/SwiftShader; --no-sandbox under xvfb on Linux CI hosts. */
export function electronFlags(): string[] {
  const f = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'];
  if (process.platform === 'linux' && process.env.HARNESS_DESKTOP_E2E_SANDBOX !== '1') f.push('--no-sandbox');
  return f;
}

export async function startMock(o: { port?: number; stateId?: string; scenario?: ScenarioName } = {}): Promise<MockExtra & { close(): Promise<void> }> {
  return startMockRaw(o);
}

/** A temp install root with the fake harness (PLAN 7.5). Returns its path. */
export function fakeInstall(o: NonNullable<LaunchOpts['fakeHarness']> & { scenario?: ScenarioName; update?: 'none' | 'available' } = {}): string {
  const root = mkdtempSync(join(tmpdir(), 'harness-fake-'));
  mkdirSync(join(root, 'serve'), { recursive: true });
  mkdirSync(join(root, 'state'), { recursive: true });
  writeFileSync(join(root, 'serve', 'pub.py'), '# fake\n');
  const dir = join(DESKTOP, 'e2e', 'fake-harness');
  writeFileSync(join(root, 'harness'), readFileSync(join(dir, 'harness'), 'utf8').replace('${FAKE_HARNESS_DIR:-$here}', dir.replace(/(["\\$`])/g, '\\$1')), { mode: 0o755 });
  if (o.env !== 'missing') writeFileSync(join(root, '.env'), '# fake .env\n');
  writeFileSync(join(root, '.env.example'), '# fake example\n');
  writeFileSync(join(root, 'fake.json'), JSON.stringify({ env: o.env || 'ok', unlock: o.unlock || 'ok', serve: o.serve || 'ok',
    update: o.update || 'none', scenario: o.scenario || 'healthy', config: {}, calls: [] }, null, 2));
  return root;
}
export function fakeState(root: string): { config: Record<string, string>; calls: string[][]; [k: string]: unknown } {
  return JSON.parse(readFileSync(join(root, 'fake.json'), 'utf8'));
}

export interface Launched { app: ElectronApplication; win: Page; userData: string; root: string | null }

/** Launch the built app (out/). attach (default true): HARNESS_DESKTOP_SERVE_URL = mock; else a fake install root. */
export async function launchApp(mock: MockServe | null, opts: LaunchOpts = {}, extraEnv: Record<string, string> = {}): Promise<Launched> {
  const userData = opts.userData || mkdtempSync(join(tmpdir(), 'harness-ud-'));
  const attach = opts.attach ?? !opts.fakeHarness;
  let root: string | null = null;
  const env: Record<string, string> = { ...(process.env as Record<string, string>), HARNESS_DESKTOP_E2E: '1', HARNESS_DESKTOP_USER_DATA: userData,
    HARNESS_DESKTOP_NO_UPDATE: '1', HARNESS_DESKTOP_LOG_LEVEL: 'debug', ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
  delete env.HARNESS_DESKTOP_SERVE_URL; delete env.HARNESS_DESKTOP_ROOT; delete env.ELECTRON_RUN_AS_NODE;
  if (attach && mock) env.HARNESS_DESKTOP_SERVE_URL = mock.url;
  if (!attach) {
    root = fakeInstall(opts.fakeHarness || {});
    env.HARNESS_DESKTOP_ROOT = root;
    env.HARNESS_DESKTOP_SERVE_PORT = String(20000 + Math.floor(Math.random() * 20000));
  }
  Object.assign(env, extraEnv);
  const app = await _electron.launch({ executablePath: ELECTRON, args: [...electronFlags(), DESKTOP, ...(opts.args || [])], env, cwd: DESKTOP, timeout: 30000 });
  const win = await app.firstWindow({ timeout: 30000 });
  return { app, win, userData, root };
}

/** Wait for L3's data-ready mark; when the renderer leg has not mounted yet, the window's load is enough. */
export async function waitReady(win: Page, timeout = 15000): Promise<'data-ready' | 'loaded'> {
  await win.waitForLoadState('load', { timeout });
  const hasShell = await win.evaluate(() => !!document.querySelector('[data-testid], #app, #root')).catch(() => false);
  if (!hasShell) return 'loaded';
  await win.waitForSelector('html[data-ready="1"]', { timeout });
  return 'data-ready';
}

type Fixtures = { mock: MockExtra; app: ElectronApplication; win: Page; launch: (opts?: LaunchOpts) => Promise<Launched> };

export const test = base.extend<Fixtures>({
  mock: async ({}, use) => {
    const m = await startMock();
    await use(m);
    await m.close();
  },
  launch: async ({ mock }, use) => {
    const opened: Launched[] = [];
    await use(async (opts = {}) => { const l = await launchApp(mock, opts, opts.env); opened.push(l); return l; });
    for (const l of opened) {
      await l.app.close().catch(() => {});
      if (!process.env.HARNESS_DESKTOP_E2E_KEEP) { rmSync(l.userData, { recursive: true, force: true }); if (l.root) rmSync(l.root, { recursive: true, force: true }); }
    }
  },
  app: async ({ launch }, use) => { const l = await launch(); await use(l.app); },
  win: async ({ app }, use) => { await use(await app.firstWindow()); },
});
export { expect };

/** Evaluate in main: the e2e hooks object (HARNESS_DESKTOP_E2E=1 only). */
export async function mainHooks<T>(app: ElectronApplication, fn: (h: any) => T | Promise<T>): Promise<T> {
  return app.evaluate(async (_e, src) => {
    const h = (globalThis as any).__harnessE2E;
    // eslint-disable-next-line no-new-func
    return await (new Function('h', `return (${src})(h)`))(h);
  }, fn.toString()) as Promise<T>;
}
