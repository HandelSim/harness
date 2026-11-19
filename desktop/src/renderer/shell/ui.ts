// Shell UI state: palette, dialogs, popovers, sidebar, zen. Overlays render from here.
import { signal } from '@preact/signals';
import type { ComponentChildren } from 'preact';
import { appState, patchState } from '../core/app';

export const paletteOpen = signal<false | { prefix: string }>(false);
export const dialog = signal<ComponentChildren | null>(null);
export const popover = signal<null | 'backend' | 'bg' | 'key'>(null);
export const zen = signal(false);
export const findRequest = signal(0);

export function openDialog(c: ComponentChildren): void { dialog.value = c; }
export function closeDialog(): void { dialog.value = null; }

export function sidebarOpen(): boolean { return appState.value?.shell?.sidebarOpen ?? true; }
export function toggleSidebar(): void {
  const s = appState.value?.shell;
  patchState({ shell: { ...(s ?? defaultsShell()), sidebarOpen: !sidebarOpen() } });
}
export function setSidebarWidth(w: number): void {
  const s = appState.value?.shell ?? defaultsShell();
  patchState({ shell: { ...s, sidebar: Math.max(200, Math.min(420, Math.round(w))) } });
}
export function defaultsShell() {
  return { sidebar: 260, sidebarOpen: true, collapsed: {} as Record<string, boolean>, activeView: 'home' };
}
