// CLI runner: runs `bash <root>/harness ...` with argv arrays (never a shell string), captures or streams output.
import { spawn, type ChildProcess } from 'node:child_process';
import type { ConfigRow } from '../shared/api';
import type { HarnessUpdateState } from '../shared/ipc';

export interface RunResult { code: number; stdout: string; stderr: string; timedOut: boolean }

export function cliEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  return { ...process.env, NO_COLOR: '1', TERM: 'dumb', HARNESS_OPEN: '0', ...extra };
}

export function runHarness(root: string, args: string[], o: { timeoutMs?: number; env?: Record<string, string>; cwd?: string } = {}): Promise<RunResult> {
  return new Promise((resolve) => {
    let out = ''; let err = ''; let timedOut = false;
    let child: ChildProcess;
    try {
      child = spawn('bash', [`${root}/harness`, ...args], { cwd: o.cwd || root, env: cliEnv(o.env), stdio: ['ignore', 'pipe', 'pipe'] });
    } catch (e) { resolve({ code: 127, stdout: '', stderr: String(e), timedOut: false }); return; }
    const cap = (s: string, c: Buffer) => (s.length > 2_000_000 ? s : s + c.toString('utf8'));
    child.stdout!.on('data', (c: Buffer) => { out = cap(out, c); });
    child.stderr!.on('data', (c: Buffer) => { err = cap(err, c); });
    const t = setTimeout(() => { timedOut = true; child.kill('SIGTERM'); setTimeout(() => child.kill('SIGKILL'), 2000).unref(); }, o.timeoutMs ?? 60000);
    child.on('error', (e) => { err += String(e); });
    child.on('close', (code, sig) => { clearTimeout(t); resolve({ code: code ?? (sig ? 128 : 1), stdout: out, stderr: err, timedOut }); });
  });
}

/** Streams a job's output; returns the child so callers can kill it. */
export function startJob(root: string, args: string[], onChunk: (s: string) => void, onExit: (code: number) => void,
  env: Record<string, string> = {}): ChildProcess | null {
  try {
    const child = spawn('bash', [`${root}/harness`, ...args], { cwd: root, env: cliEnv(env), stdio: ['ignore', 'pipe', 'pipe'] });
    const f = framer(onChunk);
    child.stdout!.on('data', f); child.stderr!.on('data', f);
    child.on('error', (e) => onChunk(`${e}\n`));
    child.on('close', (code) => { f.flush(); onExit(code ?? 1); });
    return child;
  } catch (e) { onChunk(String(e)); onExit(127); return null; }
}

/** Job output framing: decode UTF-8 safely across chunk edges, emit at most every 50 ms. */
export function framer(emit: (s: string) => void, everyMs = 50): ((c: Buffer) => void) & { flush(): void } {
  const dec = new TextDecoder('utf-8');
  let buf = ''; let timer: NodeJS.Timeout | null = null;
  const flush = () => { if (timer) { clearTimeout(timer); timer = null; } const tail = dec.decode(); if (tail) buf += tail; if (buf) { emit(buf); buf = ''; } };
  const fn = ((c: Buffer) => {
    buf += dec.decode(c, { stream: true });
    if (!timer) timer = setTimeout(() => { timer = null; if (buf) { emit(buf); buf = ''; } }, everyMs);
  }) as ((c: Buffer) => void) & { flush(): void };
  fn.flush = flush;
  return fn;
}

/** `harness config list --tsv`: KEY \t value \t secret(1/0) \t description. */
export function parseConfigTsv(text: string): ConfigRow[] {
  const rows: ConfigRow[] = [];
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    const parts = line.replace(/\r$/, '').split('\t');
    if (parts.length < 3 || !/^[A-Z][A-Z0-9_]*$/.test(parts[0])) continue;
    rows.push({ key: parts[0], value: parts[1] ?? '', secret: parts[2] === '1', desc: parts.slice(3).join('\t'), group: groupOf(parts[0]) });
  }
  return rows;
}

function groupOf(key: string): string {
  if (/^(PROXY|OAI|DEFAULT_MODEL|CHATGPT)/.test(key)) return 'model';
  if (/^(TRACKER|JIRA)/.test(key)) return 'tracker';
  if (/^QUEUE/.test(key)) return 'queue';
  return 'general';
}

/** Chars of a masked secret value "(set, N chars)"; 0 when unset. */
export function maskedChars(value: string): number {
  const m = /^\(set, (\d+) chars\)$/.exec(value.trim());
  return m ? Number(m[1]) : value ? value.length : 0;
}

/** Required .env values for the openai backend, same rule as the CLI's _openai_backend_missing. */
export function missingRequired(rows: ConfigRow[]): string[] {
  const val = (k: string) => rows.find((r) => r.key === k)?.value ?? '';
  const has = (k: string) => val(k) !== '' && val(k) !== '(not set)';
  const req: string[] = [];
  const anyOai = has('OAI_API_URL') || has('OAI_API_KEY') || has('OAI_MODELS');
  if (anyOai) req.push('OAI_API_URL', 'OAI_API_KEY', 'OAI_MODELS');
  if (has('PROXY_API_URL') || has('PROXY_API_KEY') || !anyOai) {
    req.push('PROXY_API_URL', 'PROXY_API_KEY');
    if (!anyOai) req.push('DEFAULT_MODEL_NAME');
  }
  // Only keys the listing knows about can be judged; unknown keys are not reported as missing.
  return req.filter((k) => rows.some((r) => r.key === k) && !has(k));
}

/** `harness check-updates` output -> state. Matches the CLI's wording loosely; unknown text keeps available=null. */
export function parseCheckUpdates(text: string, code: number, now: number): HarnessUpdateState {
  const t = text.replace(/\x1b\[[0-9;]*m/g, '');
  // Wording of cmd_check_updates: "up to date with origin/<branch> (<sha7>)" / "update available on <branch>; run 'harness upgrade'".
  const branch = /up to date with origin\/([\w./-]+)/.exec(t)?.[1] ?? /update available on ([\w./-]+?);/.exec(t)?.[1] ?? '';
  const local = /origin\/[\w./-]+ \(([0-9a-f]{7,40})\)/.exec(t)?.[1] ?? '';
  let available: boolean | null = null;
  if (/up to date with origin\//.test(t)) available = false;
  else if (/update available on /.test(t)) available = true;
  return { branch, local, available, text: t.trim().slice(-4000), checkedAt: now };
}
