// Append-only app log in userData/logs/app.log. Secrets are never passed here (callers redact).
import { appendFileSync, mkdirSync, statSync, renameSync } from 'node:fs';
import { join } from 'node:path';

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 } as const;
type Level = keyof typeof LEVELS;
let dir = '';
let min: number = LEVELS.info;

export function initLog(logDir: string, level?: string): void {
  dir = logDir;
  mkdirSync(dir, { recursive: true });
  if (level && level in LEVELS) min = LEVELS[level as Level];
}

export function logDir(): string { return dir; }

export function log(level: Level, msg: string): void {
  if (LEVELS[level] < min) return;
  const line = `${new Date().toISOString()} ${level} ${msg.replace(/\n/g, ' | ').slice(0, 4000)}\n`;
  if (!dir) { process.stderr.write(line); return; }
  const file = join(dir, 'app.log');
  try {
    try { if (statSync(file).size > 5 * 1024 * 1024) renameSync(file, file + '.1'); } catch { /* none yet */ }
    appendFileSync(file, line, { mode: 0o600 });
  } catch { process.stderr.write(line); }
}
