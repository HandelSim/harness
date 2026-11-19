// The one IPC wrapper: sender check, payload validation, never throws to the renderer.
import { ipcMain, type IpcMainInvokeEvent, type IpcMainEvent, type WebContents } from 'electron';
import { senderOk } from './security';
import { log } from './log';

export class BadPayload extends Error {}
export const unavailable = (what: string) => ({ ok: false as const, error: { code: 'unavailable', message: `${what} is not available` } });

type Check<P> = (p: unknown) => P;

/** invoke channel. `check` throws BadPayload (or returns the typed payload). */
export function handle<P, R>(channel: string, check: Check<P>, fn: (p: P, e: IpcMainInvokeEvent) => R | Promise<R>,
  onError: (msg: string, code: string) => unknown = (message, code) => ({ ok: false, error: { code, message } })): void {
  ipcMain.handle(channel, async (e, raw) => {
    if (!senderOk(e.senderFrame?.url)) {
      log('warn', `ipc ${channel} refused: sender ${e.senderFrame?.url}`);
      return onError('sender not allowed', 'sender');
    }
    let p: P;
    try { p = check(raw); } catch (err) { return onError(String((err as Error).message || err), 'bad-request'); }
    try { return await fn(p, e); } catch (err) {
      log('error', `ipc ${channel}: ${(err as Error)?.stack || err}`);
      return onError(String((err as Error)?.message || err), 'internal');
    }
  });
}

/** fire-and-forget channel (send). Bad payloads are dropped. */
export function listen<P>(channel: string, check: Check<P>, fn: (p: P, e: IpcMainEvent) => void): void {
  ipcMain.on(channel, (e, raw) => {
    if (!senderOk(e.senderFrame?.url)) return;
    let p: P;
    try { p = check(raw); } catch { return; }
    try { fn(p, e); } catch (err) { log('error', `ipc ${channel}: ${(err as Error)?.stack || err}`); }
  });
}

let targets: () => WebContents[] = () => [];
export function setEventTargets(fn: () => WebContents[]): void { targets = fn; }
/** Recorder for e2e (secret-leak test reads it via app.evaluate). */
export const sentLog: { channel: string; json: string }[] = [];
let recordSent = false;
export function recordEvents(on: boolean): void { recordSent = on; }

export function emit(channel: string, payload: unknown): void {
  if (recordSent && channel !== 'pty:data') { sentLog.push({ channel, json: JSON.stringify(payload) }); if (sentLog.length > 5000) sentLog.shift(); }
  for (const wc of targets()) if (!wc.isDestroyed()) wc.send(channel, payload);
}

// ---- small validators ----
export const v = {
  none: (p: unknown) => p,
  obj(p: unknown): Record<string, unknown> {
    if (typeof p !== 'object' || p === null || Array.isArray(p)) throw new BadPayload('payload must be an object');
    return p as Record<string, unknown>;
  },
  str(x: unknown, name: string, max = 100000): string {
    if (typeof x !== 'string' || x.length > max) throw new BadPayload(`${name} must be a string`);
    return x;
  },
  optStr(x: unknown, name: string, max = 100000): string | undefined {
    return x === undefined || x === null ? undefined : v.str(x, name, max);
  },
  int(x: unknown, name: string, lo: number, hi: number): number {
    if (typeof x !== 'number' || !Number.isInteger(x) || x < lo || x > hi) throw new BadPayload(`${name} out of range`);
    return x;
  },
  oneOf<T extends string>(x: unknown, name: string, vals: readonly T[]): T {
    if (!vals.includes(x as T)) throw new BadPayload(`${name} must be one of ${vals.join(', ')}`);
    return x as T;
  },
};
