// Key lifecycle math (upstream-api.md: a key locks 14 days after creation or its last unlock; fixed expiry).
export const DAY = 86400000;
export const LOCK_DAYS = 14;

export function locksAt(createdAt: number | null, lastUnlockAt: number | null): number | null {
  const base = Math.max(createdAt ?? -Infinity, lastUnlockAt ?? -Infinity);
  return Number.isFinite(base) ? base + LOCK_DAYS * DAY : null;
}

export interface Reminder { at: number; what: 'lock' | 'expiry'; text: string }
/** Notifications: 48 h before a lock; 7 days and 1 day before expiry. Only future ones. */
export function reminders(k: { label: string; createdAt: number | null; lastUnlockAt: number | null; expiresAt: number | null }, now: number): Reminder[] {
  const out: Reminder[] = [];
  const l = locksAt(k.createdAt, k.lastUnlockAt);
  if (l !== null && l - 2 * DAY > now) out.push({ at: l - 2 * DAY, what: 'lock', text: `${k.label} locks in 2 days` });
  if (k.expiresAt !== null) {
    if (k.expiresAt - 7 * DAY > now) out.push({ at: k.expiresAt - 7 * DAY, what: 'expiry', text: `${k.label} expires in 7 days` });
    if (k.expiresAt - DAY > now) out.push({ at: k.expiresAt - DAY, what: 'expiry', text: `${k.label} expires tomorrow` });
  }
  return out.sort((a, b) => a.at - b.at);
}
