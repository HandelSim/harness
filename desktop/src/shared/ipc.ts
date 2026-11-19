// FILE: desktop/src/shared/ipc.ts
import type { ActionResult, ConfigRow, ReadResult, Topic } from './api';

export type BackendPhase = 'locating' | 'no-install' | 'needs-config' | 'starting' | 'up'
  | 'reconnecting' | 'down' | 'needs-login' | 'incompatible';
export interface BackendState {
  phase: BackendPhase; root: string | null; url: string | null; port: number | null;
  owned: boolean; pid: number | null; stale: boolean; apiVersion: number | null;
  missing: string[];                      // needs-config: required .env keys that are empty ('.env' when the file is absent)
  hostConsent: boolean;
  problem: { code: string; message: string; unlockUrl?: string } | null;
  logTail: string[];                      // last lines of the owned serve's output
  since: number;
}
export type BackendAction =
  | { action: 'start' | 'restart' | 'stop' | 'retry' | 'create-env' | 'consent-host' | 'create-code' }
  | { action: 'set-root'; root: string }
  | { action: 'login'; code: string };

export interface AppInfo {
  version: string; platform: NodeJS.Platform; arch: string; packaged: boolean; e2e: boolean;
  userData: string; home: string; electron: string; chrome: string;
  keyStorage: { secure: boolean; backend: string };
  reducedMotion: boolean; onBattery: boolean;
}

export type KeyKind = 'PROXY_API_KEY' | 'OAI_API_KEY' | 'TRACKER_TOKEN' | 'CHATGPT_COOKIE_STRING' | 'ACCESS_CODE';
export type ProbeState = 'ok' | 'locked' | 'rejected' | 'unknown';
export interface KeyProbe { state: ProbeState; at: number; message: string; unlockUrl?: string }
export interface KeyMeta {
  id: string; kind: KeyKind; label: string; last4: string; active: boolean; superseded: boolean; team: boolean;
  addedAt: number; createdAt: number | null; expiresAt: number | null; lastUnlockAt: number | null;
  locksAt: number | null;                 // computed: max(createdAt, lastUnlockAt) + 14 days; null when unknown
  probe: KeyProbe | null;
}
export interface KeyVaultState {
  secure: boolean; backend: string; storeInactive: boolean;
  keys: KeyMeta[];
  env: Partial<Record<KeyKind, { set: boolean; chars: number; inVault: boolean }>>;  // what .env holds, from config list
}
export type KeyOpResult = { ok: true; state: KeyVaultState; msg: string; reverted?: boolean }
  | { ok: false; error: { code: string; message: string }; state: KeyVaultState };

export interface PtySpec {
  kind: 'agent' | 'host' | 'shell' | 'plain'; cwd: string;
  yolo?: boolean; normal?: boolean; thoughts?: boolean; net?: boolean;
  ticket?: string; resume?: 'last' | { session: string };
}
export interface PtyInfo { id: string; spec: PtySpec; pid: number; startedAt: number; exited: { code: number; signal: number } | null }

export interface AppUpdateState {
  phase: 'unsupported' | 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'error';
  current: string; next: string | null; notes: string; percent: number; reason: string; checkedAt: number | null;
  releasePage: string;
}
export interface HarnessUpdateState { branch: string; local: string; available: boolean | null; text: string; checkedAt: number | null }
export interface PathInfo { exists: boolean; dir: boolean; git: { branch: string } | null; entries?: string[] }

export interface Settings {
  appearance: { theme: 'dark' | 'light' | 'system'; accent: string; density: 'comfortable' | 'compact';
    uiScale: number; glass: boolean; seeThrough: number; reduceMotion: 'system' | 'on' | 'off' };
  background: { config: import('./bg').BgConfig; onBlur: 'run' | 'dim' | 'pause'; onBattery: 'run' | 'static';
    rotate: 'never' | 'daily' | 'launch' };
  terminal: { fontFamily: string; fontSize: number; lineHeight: number; cursorBlink: boolean; scrollback: number;
    confirmClose: boolean; confirmPaste: boolean; webgl: boolean };
  sessions: { restore: 'tabs' | 'tabs-and-resume' | 'none'; defaultKind: PtySpec['kind'] };
  queue: { startMode: 'container' | 'host' | 'single' };
  backend: { installRoot: string | null; port: number; onQuit: 'keep-if-busy' | 'stop' | 'keep'; hostConsent: boolean };
  updates: { auto: boolean; harnessCheck: boolean };
  notifications: { needsInput: boolean; ticketDone: boolean; runEnded: boolean; key: boolean; update: boolean };
  keymap: Record<string, string>;          // command id -> chord; only overrides are stored
}
export interface AppState {
  version: 1;
  window: { x?: number; y?: number; width: number; height: number; maximized: boolean; fullscreen: boolean };
  shell: { sidebar: number; sidebarOpen: boolean; collapsed: Record<string, boolean>; activeView: string };
  dock: unknown;                           // dockview toJSON()
  panels: Record<string, unknown>;         // panel id -> that panel's serialize()
  closed: unknown[];                       // reopen stack (<= 20)
  drafts: Record<string, string>;          // "<scope>:<id>:<field>" -> text
  recents: { projects: string[]; commands: string[] };
}

export interface BridgeEvents {
  'backend:state': BackendState; 'live:update': { topic: Topic; sig: string; data: unknown };
  'live:health': { ok: boolean; failures: number; nextInMs: number };
  'job:output': { id: string; chunk: string }; 'job:exit': { id: string; code: number };
  'keys:changed': KeyVaultState; 'settings:changed': Settings;
  'pty:data': { id: string; data: string }; 'pty:exit': { id: string; code: number; signal: number };
  'update:state': AppUpdateState; 'window:state': { maximized: boolean; fullscreen: boolean; focused: boolean; visible: boolean };
  'power:state': { onBattery: boolean; idle: boolean }; 'notify:click': { route: string };
  'menu:command': { command: string }; 'app:will-quit': Record<string, never>;
}
export interface Bridge {
  on<K extends keyof BridgeEvents>(event: K, cb: (payload: BridgeEvents[K]) => void): () => void;
  app: { info(): Promise<AppInfo> };
  backend: { get(): Promise<BackendState>; action(a: BackendAction): Promise<BackendState> };
  api: { get<T>(path: string, query?: Record<string, string | number | boolean>, sig?: string): Promise<ReadResult<T>>;
    post(path: string, body?: Record<string, unknown>): Promise<ActionResult>;
    bytes(path: string): Promise<{ ok: boolean; mime: string; data: Uint8Array }> };
  live: { set(topics: Topic[]): Promise<void> };
  harness: { configList(): Promise<{ ok: boolean; rows: ConfigRow[]; message: string }>;
    configSet(key: string, value: string): Promise<{ ok: boolean; msg: string }>;
    job(job: 'doctor' | 'check-updates' | 'upgrade' | 'serve-stop'): Promise<{ id: string }>;
    updateGet(): Promise<HarnessUpdateState> };
  keys: { list(): Promise<KeyVaultState>;
    add(k: { kind: KeyKind; label: string; secret: string; createdAt?: number; expiresAt?: number; team?: boolean; activate?: boolean }): Promise<KeyOpResult>;
    update(k: { id: string; label?: string; createdAt?: number | null; expiresAt?: number | null; team?: boolean }): Promise<KeyOpResult>;
    remove(id: string): Promise<KeyOpResult>; activate(id: string): Promise<KeyOpResult>;
    probe(target: { id: string } | { active: KeyKind }): Promise<KeyProbe>; clearActive(kind: KeyKind): Promise<KeyOpResult> };
  settings: { get(): Promise<Settings>; set(patch: DeepPartial<Settings>): Promise<Settings> };
  state: { get(): Promise<AppState>; set(patch: Partial<AppState>): Promise<void> };
  pty: { spawn(spec: PtySpec, cols: number, rows: number): Promise<{ ok: true; id: string } | { ok: false; error: { code: string; message: string } }>;
    write(id: string, data: string): void; resize(id: string, cols: number, rows: number): void;
    ack(id: string, chars: number): void; kill(id: string): void;
    list(): Promise<PtyInfo[]>; attach(id: string): Promise<{ info: PtyInfo; replay: string } | null> };
  web: { url(target: 'serve' | 'opencode', path: string): Promise<{ ok: boolean; url: string; partition: string }>;
    theme(css: string): Promise<void> };
  update: { get(): Promise<AppUpdateState>; check(): Promise<AppUpdateState>; download(): Promise<AppUpdateState>; install(): Promise<void> };
  shell: { openExternal(url: string): Promise<boolean>; showItem(path: string): Promise<void>;
    pickFolder(defaultPath?: string): Promise<string | null>; pathInfo(path: string, list?: boolean): Promise<PathInfo>;
    saveFile(name: string, text: string): Promise<string | null> };
  window: { action(action: 'minimize' | 'maximize' | 'close' | 'fullscreen' | 'zoom', value?: number): Promise<void> };
  notify: { show(n: { id: string; title: string; body: string; route: string }): Promise<void> };
  log(level: 'info' | 'warn' | 'error', msg: string): void;
}
export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? DeepPartial<T[K]> : T[K] };
declare global { interface Window { harness: Bridge } }
