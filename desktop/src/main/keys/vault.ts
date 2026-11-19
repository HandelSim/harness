// Key vault (D11): metadata + (optionally) encrypted secrets in userData/keys.json (mode 600); the active key per
// kind is written to .env with `harness config set`. Secrets never leave main after entry.
import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import type { SafeStorage } from 'electron';
import type { KeyKind, KeyMeta, KeyOpResult, KeyProbe, KeyVaultState } from '../../shared/ipc';
import type { ConfigRow } from '../../shared/api';
import { loadJson, saveJson, isObj } from '../store';
import { runHarness, parseConfigTsv, maskedChars, cliEnv, type RunResult } from '../cli';
import { locksAt } from './lifecycle';
import { classifyUnlock, probeDirect } from './probe';
import { log } from '../log';
import type { handle as Handle, v as V } from '../ipc';

export const KINDS: KeyKind[] = ['PROXY_API_KEY', 'OAI_API_KEY', 'TRACKER_TOKEN', 'CHATGPT_COOKIE_STRING', 'ACCESS_CODE'];
interface Entry extends Omit<KeyMeta, 'locksAt'> { enc: string | null }
interface File { version: 1; storeInactive: boolean; keys: Entry[] }

export interface VaultDeps { file: string; safe: SafeStorage; root: () => string | null; emit: (s: KeyVaultState) => void;
  run?: typeof runHarness; runStdin?: typeof runHarnessStdin; probe?: typeof probeDirect }

/** M8: `harness <args>` with `input` written to its stdin, so a secret never sits on argv (`ps`, /proc/<pid>/cmdline). */
export function runHarnessStdin(root: string, args: string[], input: string, o: { timeoutMs?: number } = {}): Promise<RunResult> {
  return new Promise((resolve) => {
    let out = ''; let err = ''; let timedOut = false;
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn('bash', [`${root}/harness`, ...args], { cwd: root, env: cliEnv(), stdio: ['pipe', 'pipe', 'pipe'] });
    } catch (e) { resolve({ code: 127, stdout: '', stderr: String(e), timedOut: false }); return; }
    child.stdout!.on('data', (c: Buffer) => { if (out.length < 200_000) out += c.toString('utf8'); });
    child.stderr!.on('data', (c: Buffer) => { if (err.length < 200_000) err += c.toString('utf8'); });
    child.stdin!.on('error', () => { /* EPIPE: the CLI exited early; its exit code says why */ });
    child.stdin!.end(input + '\n');
    const t = setTimeout(() => { timedOut = true; child.kill('SIGTERM'); setTimeout(() => child.kill('SIGKILL'), 2000).unref(); }, o.timeoutMs ?? 60000);
    child.on('error', (e) => { err += String(e); });
    child.on('close', (code, sig) => { clearTimeout(t); resolve({ code: code ?? (sig ? 128 : 1), stdout: out, stderr: err, timedOut }); });
  });
}

/** L10: the last 4 chars are shown only for long secrets; a short one (e.g. an access code) would give away too much. */
export const LAST4_MIN = 16;
export function last4Of(secret: string): string { return secret.length >= LAST4_MIN ? secret.slice(-4) : ''; }

export class KeyVault {
  private f: File;
  private env: KeyVaultState['env'] = {};
  private rows: ConfigRow[] = [];
  private mem = new Map<string, string>();      // secrets held only for this run (insecure backend)
  private run: typeof runHarness; private runIn: typeof runHarnessStdin; private probeD: typeof probeDirect;
  constructor(private d: VaultDeps) {
    const r = loadJson<File>(d.file, () => ({ version: 1, storeInactive: false, keys: [] }), (raw) =>
      isObj(raw) && raw.version === 1 && Array.isArray(raw.keys) ? (raw as unknown as File) : null);
    this.f = r.value;
    if (r.recovered) log('warn', `keys.json unreadable, moved to ${r.recovered}`);
    this.run = d.run || runHarness; this.runIn = d.runStdin || runHarnessStdin; this.probeD = d.probe || probeDirect;
    // L10 migration: entries written before `chars` existed showed last4 of any secret; keep it only when provably long
    let fixed = false;
    for (const e of this.f.keys) {
      if (typeof e.chars === 'number') continue;
      const sec = this.secretOf(e);
      e.chars = sec ? sec.length : undefined;
      const l4 = sec ? last4Of(sec) : '';
      if (e.last4 !== l4) { e.last4 = l4; fixed = true; }
    }
    if (fixed) { try { saveJson(d.file, this.f, 0o600); } catch (e) { log('warn', `keys.json migrate: ${e}`); } }
  }

  storageInfo(): { secure: boolean; backend: string } {
    let backend = 'unknown'; let secure = false;
    try {
      const avail = this.d.safe.isEncryptionAvailable();
      backend = process.platform === 'linux' ? this.d.safe.getSelectedStorageBackend() : avail ? 'os' : 'none';
      secure = avail && backend !== 'basic_text' && backend !== 'unknown';
    } catch { /* not ready */ }
    return { secure, backend };
  }

  state(): KeyVaultState {
    const s = this.storageInfo();
    return { secure: s.secure, backend: s.backend, storeInactive: this.f.storeInactive,
      keys: this.f.keys.map(({ enc: _e, ...k }) => ({ ...k, locksAt: k.kind === 'ACCESS_CODE' ? null : locksAt(k.createdAt, k.lastUnlockAt) })),
      env: this.env };
  }

  private save(): void { saveJson(this.d.file, this.f, 0o600); this.d.emit(this.state()); }
  private encrypt(secret: string): string | null {
    try { return this.d.safe.encryptString(secret).toString('base64'); } catch { return null; }
  }
  private secretOf(e: Entry): string | null {
    if (this.mem.has(e.id)) return this.mem.get(e.id)!;
    if (!e.enc) return null;
    try { return this.d.safe.decryptString(Buffer.from(e.enc, 'base64')); } catch { return null; }
  }

  async refreshEnv(): Promise<void> {
    const root = this.d.root();
    if (!root) return;
    const r = await this.run(root, ['config', 'list', '--tsv'], { timeoutMs: 30000 });
    if (r.code !== 0) return;
    this.rows = parseConfigTsv(r.stdout);
    const env: KeyVaultState['env'] = {};
    for (const k of KINDS) {
      const row = this.rows.find((x) => x.key === k);
      if (!row) continue;
      const chars = maskedChars(row.value);
      env[k] = { set: chars > 0, chars, inVault: this.f.keys.some((e) => e.kind === k && e.active) };
    }
    this.env = env;
  }

  accessCode(): string | null {
    const e = this.f.keys.find((k) => k.kind === 'ACCESS_CODE' && k.active);
    return e ? this.secretOf(e) : null;
  }
  saveAccessCode(code: string): void {
    for (const k of this.f.keys) if (k.kind === 'ACCESS_CODE') k.active = false;
    const e = this.entry('ACCESS_CODE', 'access code', code, { active: true });
    this.f.keys = this.f.keys.filter((k) => k.kind !== 'ACCESS_CODE');
    this.f.keys.push(e);
    this.save();
  }

  private entry(kind: KeyKind, label: string, secret: string, o: { active: boolean; createdAt?: number; expiresAt?: number; team?: boolean }): Entry {
    const id = randomUUID();
    const store = this.storageInfo().secure || this.f.storeInactive;
    const enc = store ? this.encrypt(secret) : null;
    if (!enc) this.mem.set(id, secret);
    return { id, kind, label: label.slice(0, 80) || kind, last4: last4Of(secret), chars: secret.length, active: o.active, superseded: false, team: !!o.team,
      addedAt: Date.now(), createdAt: o.createdAt ?? Date.now(), expiresAt: o.expiresAt ?? null, lastUnlockAt: null, probe: null, enc };
  }

  private fail(code: string, message: string): KeyOpResult { return { ok: false, error: { code, message }, state: this.state() }; }

  private async writeEnv(kind: KeyKind, secret: string): Promise<{ ok: boolean; msg: string }> {
    const root = this.d.root();
    if (!root) return { ok: false, msg: 'no harness install' };
    const r = await this.runIn(root, ['config', 'set', kind, '--stdin'], secret, { timeoutMs: 30000 });
    // never echo the CLI output verbatim: it could contain the value
    return { ok: r.code === 0, msg: r.code === 0 ? `${kind} written to .env` : 'harness config set failed' };
  }

  async add(k: { kind: KeyKind; label: string; secret: string; createdAt?: number; expiresAt?: number; team?: boolean; activate?: boolean }): Promise<KeyOpResult> {
    if (!k.secret || /[\r\n]/.test(k.secret) || k.secret.length > 8000) return this.fail('bad-secret', 'the secret must be one line, 1-8000 chars');
    if (!k.activate && !this.storageInfo().secure && !this.f.storeInactive && k.kind !== 'ACCESS_CODE')
      return this.fail('insecure', 'no OS keyring: only the active key can be kept (in .env). Save and make it active, or opt in to obfuscated storage.');
    const e = this.entry(k.kind, k.label, k.secret, { active: false, createdAt: k.createdAt, expiresAt: k.expiresAt, team: k.team });
    this.f.keys.push(e);
    this.save();
    if (k.activate) return this.activate(e.id);
    return { ok: true, state: this.state(), msg: 'key stored' };
  }

  update(k: { id: string; label?: string; createdAt?: number | null; expiresAt?: number | null; team?: boolean }): KeyOpResult {
    const e = this.f.keys.find((x) => x.id === k.id);
    if (!e) return this.fail('not-found', 'no such key');
    if (k.label !== undefined) e.label = k.label.slice(0, 80);
    if (k.createdAt !== undefined) e.createdAt = k.createdAt;
    if (k.expiresAt !== undefined) e.expiresAt = k.expiresAt;
    if (k.team !== undefined) e.team = k.team;
    this.save();
    return { ok: true, state: this.state(), msg: 'saved' };
  }

  remove(id: string): KeyOpResult {
    const e = this.f.keys.find((x) => x.id === id);
    if (!e) return this.fail('not-found', 'no such key');
    if (e.active && e.kind !== 'ACCESS_CODE') return this.fail('active', 'this key is active: make another key active or clear it from .env first');
    this.f.keys = this.f.keys.filter((x) => x.id !== id);
    this.mem.delete(id);
    this.save();
    return { ok: true, state: this.state(), msg: 'key removed' };
  }

  /** M9: keep inactive keys without an OS keyring (safeStorage's obfuscated `basic_text` store). Turning it off
   *  drops the stored secrets of every key from disk (held in memory for this run only), as if they had been added with it off. */
  setStoreInactive(on: boolean): KeyOpResult {
    this.f.storeInactive = on;
    const secure = this.storageInfo().secure;
    for (const e of this.f.keys) {
      if (on && !e.enc && this.mem.has(e.id)) { const enc = this.encrypt(this.mem.get(e.id)!); if (enc) { e.enc = enc; this.mem.delete(e.id); } }
      if (!on && !secure && e.enc) { const sec = this.secretOf(e); if (sec) this.mem.set(e.id, sec); e.enc = null; }
    }
    this.save();
    return { ok: true, state: this.state(), msg: on ? 'inactive keys are kept (obfuscated, not encrypted by an OS keyring)' : 'inactive keys are no longer stored on disk' };
  }

  /** W12: candidate probe, config set, canonical probe, automatic revert. */
  async activate(id: string): Promise<KeyOpResult> {
    const e = this.f.keys.find((x) => x.id === id);
    if (!e) return this.fail('not-found', 'no such key');
    const secret = this.secretOf(e);
    if (!secret) return this.fail('no-secret', 'the secret of this key is not stored in the app');
    if (e.kind === 'ACCESS_CODE') { for (const k of this.f.keys) if (k.kind === 'ACCESS_CODE') k.active = k.id === id; this.save(); return { ok: true, state: this.state(), msg: 'access code active' }; }
    await this.refreshEnv();
    const cand = await this.probeSecret(e.kind, secret);
    e.probe = cand;
    if (cand.state === 'rejected') { this.save(); return this.fail('rejected', `the key was rejected: ${cand.message}; nothing changed`); }
    const prev = this.f.keys.find((x) => x.kind === e.kind && x.active && x.id !== id) || null;
    const w = await this.writeEnv(e.kind, secret);
    if (!w.ok) { this.save(); return this.fail('config-set', w.msg); }
    const canon = await this.probeActive(e.kind);
    if (canon.state === 'rejected') {
      const ps = prev ? this.secretOf(prev) : null;
      if (prev && ps) {
        await this.writeEnv(e.kind, ps);
        e.probe = canon;
        this.save();
        return { ok: true, state: this.state(), msg: `the new key was rejected (${canon.message}); the previous key is active again`, reverted: true };
      }
      // nothing stored to go back to: the new key stays in .env, and the caller is told it does not work
      for (const k of this.f.keys) if (k.kind === e.kind) k.active = k.id === id;
      e.probe = canon;
      await this.refreshEnv();
      this.save();
      return this.fail('rejected', `the key is in .env but upstream rejects it (${canon.message}); no previous key is stored to go back to`);
    }
    for (const k of this.f.keys) if (k.kind === e.kind) { if (k.id === id) k.active = true; else if (k.active) { k.active = false; k.superseded = true; } }
    e.probe = canon.state === 'unknown' ? cand : canon;
    if (e.probe.state === 'ok' && cand.state === 'locked') e.lastUnlockAt = Date.now();
    // single-key mode: an inactive key is not kept when there is no keyring
    if (prev && !this.storageInfo().secure && !this.f.storeInactive) { prev.enc = null; this.mem.delete(prev.id); }
    await this.refreshEnv();
    this.save();
    return { ok: true, state: this.state(), msg: 'Active key changed. Revoke the old key in the upstream portal when you are ready' };
  }

  async clearActive(kind: KeyKind): Promise<KeyOpResult> {
    const root = this.d.root();
    if (!root) return this.fail('no-install', 'no harness install');
    const r = await this.run(root, ['config', 'set', kind, ''], { timeoutMs: 30000 });
    if (r.code !== 0) return this.fail('config-set', 'harness config set failed');
    for (const k of this.f.keys) if (k.kind === kind) k.active = false;
    await this.refreshEnv();
    this.save();
    return { ok: true, state: this.state(), msg: `${kind} cleared from .env` };
  }

  /** Canonical probe of the active key (`harness unlock`); only the upstream key has one. */
  async probeActive(kind: KeyKind): Promise<KeyProbe> {
    const now = Date.now();
    const root = this.d.root();
    if (!root) return { state: 'unknown', at: now, message: 'no harness install' };
    if (kind !== 'PROXY_API_KEY') return { state: 'unknown', at: now, message: 'no check for this kind of key' };
    const r = await this.run(root, ['unlock'], { timeoutMs: 60000 });
    const p = classifyUnlock(r.code, r.stdout + '\n' + r.stderr, now);
    const act = this.f.keys.find((k) => k.kind === kind && k.active);
    if (act) { if (p.state === 'ok' && act.probe?.state === 'locked') act.lastUnlockAt = now; act.probe = p; this.save(); }
    return p;
  }

  private async probeSecret(kind: KeyKind, secret: string): Promise<KeyProbe> {
    const now = Date.now();
    const val = (k: string) => this.rows.find((r) => r.key === k)?.value || '';
    if (kind === 'PROXY_API_KEY' && val('PROXY_API_URL')) return this.probeD(val('PROXY_API_URL'), secret, val('DEFAULT_MODEL_NAME'));
    if (kind === 'OAI_API_KEY' && val('OAI_API_URL')) return this.probeD(val('OAI_API_URL'), secret, val('OAI_MODELS').split(/[ ,]/)[0] || '');
    return { state: 'unknown', at: now, message: 'no check for this kind of key' };
  }

  async probe(t: { id: string } | { active: KeyKind }): Promise<KeyProbe> {
    if ('active' in t) return this.probeActive(t.active);
    const e = this.f.keys.find((x) => x.id === t.id);
    if (!e) return { state: 'unknown', at: Date.now(), message: 'no such key' };
    if (e.active) return this.probeActive(e.kind);
    const s = this.secretOf(e);
    if (!s) return { state: 'unknown', at: Date.now(), message: 'the secret is not stored' };
    await this.refreshEnv();
    e.probe = await this.probeSecret(e.kind, s);
    this.save();
    return e.probe;
  }

  register(handle: typeof Handle, v: typeof V): void {
    const kind = (x: unknown) => v.oneOf(x, 'kind', KINDS);
    const date = (x: unknown, n: string) => (x === undefined ? undefined : x === null ? null : typeof x === 'number' && Number.isFinite(x) ? x : (() => { throw new Error(`${n} must be epoch ms`); })());
    const err = (message: string, code: string) => ({ ok: false, error: { code, message }, state: this.state() });
    handle('keys:list', v.none, async () => { await this.refreshEnv(); return this.state(); }, () => this.state());
    handle('keys:add', (p) => {
      const o = v.obj(p);
      return { kind: kind(o.kind), label: v.str(o.label ?? '', 'label', 200), secret: v.str(o.secret, 'secret', 8000),
        createdAt: date(o.createdAt, 'createdAt') ?? undefined, expiresAt: date(o.expiresAt, 'expiresAt') ?? undefined,
        team: o.team === true, activate: o.activate === true };
    }, (k) => this.add(k), err);
    handle('keys:update', (p) => {
      const o = v.obj(p);
      return { id: v.str(o.id, 'id', 64), label: v.optStr(o.label, 'label', 200), createdAt: date(o.createdAt, 'createdAt'),
        expiresAt: date(o.expiresAt, 'expiresAt'), team: typeof o.team === 'boolean' ? o.team : undefined };
    }, (k) => this.update(k), err);
    handle('keys:remove', (p) => v.str(v.obj(p).id, 'id', 64), (id) => this.remove(id), err);
    handle('keys:activate', (p) => v.str(v.obj(p).id, 'id', 64), (id) => this.activate(id), err);
    handle('keys:probe', (p) => { const o = v.obj(p); return 'active' in o ? { active: kind(o.active) } : { id: v.str(o.id, 'id', 64) }; },
      (t) => this.probe(t), (message) => ({ state: 'unknown', at: Date.now(), message }));
    handle('keys:clear-active', (p) => kind(v.obj(p).kind), (k) => this.clearActive(k), err);
    handle('keys:set-store-inactive', (p) => { const on = v.obj(p).on; if (typeof on !== 'boolean') throw new Error('on must be a boolean'); return on; },
      (on) => this.setStoreInactive(on), err);
  }
}
