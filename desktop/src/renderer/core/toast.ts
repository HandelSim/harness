// Toast queue (DR (d)): bottom-right, up to 3 visible, 5 s auto-dismiss, errors persist.
import { signal } from '@preact/signals';

export interface Toast { id: number; kind: 'ok' | 'info' | 'warn' | 'err'; title: string; body?: string;
  action?: { label: string; run: () => void }; sticky?: boolean }
export const toasts = signal<Toast[]>([]);
let seq = 0;

export function toast(t: Omit<Toast, 'id'>): number {
  const id = ++seq;
  toasts.value = [...toasts.value, { ...t, id }].slice(-3);
  if (!t.sticky && t.kind !== 'err') setTimeout(() => dismiss(id), 5000);
  return id;
}
export function dismiss(id: number): void { toasts.value = toasts.value.filter((t) => t.id !== id); }
export const ok = (title: string, body?: string) => toast({ kind: 'ok', title, body });
export const fail = (title: string, body?: string) => toast({ kind: 'err', title, body });
