// The app command registry instance, default commands (W17/W18) and global key dispatch.
import { effect } from '@preact/signals';
import { CommandRegistry, pushRecent, type Command } from '../core/commands';
import { Keymap, mergeBindings, isTyping, type Binding } from '../core/keymap';
import { settings, platform, appState, patchState, patchSettings } from '../core/app';
import { bridge } from '../core/bridge';
import { VIEW_IDS, VIEW_TITLES, type ViewId } from './panels';
import { openView, closeActive, reopenClosed, cycleTab, tabN, splitActive, focusGroup, undoLayout, resetLayout, floatActive, getApi, openWeb } from './dock';
import { paletteOpen, toggleSidebar, zen, popover, findRequest } from './ui';

export const commands = new CommandRegistry();
export const keymap = new Keymap('linux');

/** Alt+1..9 view order (W18). */
export const VIEW_ORDER: ViewId[] = ['home', 'queue', 'sessions', 'findings', 'vault', 'jira', 'keys', 'settings', 'updates'];

/** Hook for the new-session dialog (set by the dialog module to avoid an import cycle). */
export const hooks: { newSession: (again?: boolean) => void; addTickets: () => void } = {
  newSession: () => openView('sessions'), addTickets: () => openView('queue'),
};

const zoomStep = (d: number) => {
  const cur = settings.value?.appearance.uiScale ?? 1;
  const next = d === 0 ? 1 : Math.max(0.8, Math.min(1.5, Math.round((cur + d) * 100) / 100));
  void patchSettings({ appearance: { uiScale: next } });
};

export function registerDefaultCommands(): void {
  const c: Command[] = [
    { id: 'palette.open', title: 'Command palette', group: 'Actions', chord: 'Mod+K, Ctrl+Shift+P', run: () => { paletteOpen.value = { prefix: '' }; } },
    { id: 'session.new', title: 'New session…', group: 'Sessions', chord: 'Mod+T', keywords: ['terminal', 'agent', 'shell'], run: () => hooks.newSession() },
    { id: 'session.again', title: 'New session (same again)', group: 'Sessions', chord: 'Mod+Shift+D', run: () => hooks.newSession(true) },
    { id: 'tab.reopen', title: 'Reopen closed tab', group: 'Layout', chord: 'Mod+Shift+T', run: () => { reopenClosed(); } },
    { id: 'tab.close', title: 'Close tab', group: 'Layout', chord: 'Mod+W', run: () => closeActive() },
    { id: 'tab.next', title: 'Next tab', group: 'Layout', chord: 'Ctrl+Tab', run: () => cycleTab(1) },
    { id: 'tab.prev', title: 'Previous tab', group: 'Layout', chord: 'Ctrl+Shift+Tab', run: () => cycleTab(-1) },
    ...Array.from({ length: 9 }, (_, i): Command => ({ id: `tab.${i + 1}`, title: `Go to tab ${i + 1}`, group: 'Layout', chord: `Mod+${i + 1}`, run: () => tabN(i + 1) })),
    { id: 'layout.splitRight', title: 'Split right', group: 'Layout', chord: 'Mod+\\', run: () => splitActive('right') },
    { id: 'layout.splitDown', title: 'Split down', group: 'Layout', chord: 'Mod+Shift+\\', run: () => splitActive('down') },
    ...(['left', 'right', 'up', 'down'] as const).flatMap((d): Command[] => {
      const k = d[0].toUpperCase() + d.slice(1);
      return [
        { id: `group.focus.${d}`, title: `Focus group ${d}`, group: 'Layout', chord: `Mod+Alt+${k}`, run: () => focusGroup(d) },
        { id: `group.move.${d}`, title: `Move tab ${d}`, group: 'Layout', chord: `Mod+Alt+Shift+${k}`, run: () => focusGroup(d, true) },
      ];
    }),
    { id: 'layout.float', title: 'Float tab', group: 'Layout', run: () => floatActive() },
    { id: 'layout.undo', title: 'Undo layout change', group: 'Layout', chord: 'Mod+Alt+Z', run: () => { undoLayout(); } },
    { id: 'layout.reset', title: 'Reset layout', group: 'Layout', run: () => resetLayout() },
    { id: 'sidebar.toggle', title: 'Toggle sidebar', group: 'Layout', chord: 'Mod+B', run: () => toggleSidebar() },
    { id: 'layout.bottom', title: 'Toggle bottom group', group: 'Layout', chord: 'Mod+J', run: () => toggleBottom() },
    { id: 'layout.zen', title: 'Zen mode', group: 'Layout', chord: 'Mod+.', run: () => { zen.value = !zen.value; } },
    ...VIEW_IDS.map((v): Command => {
      const i = VIEW_ORDER.indexOf(v);
      return { id: `view.${v}`, title: `Go to ${VIEW_TITLES[v]}`, group: 'Views', keywords: [v],
        chord: i >= 0 ? (platform.value === 'darwin' ? `Ctrl+${i + 1}` : `Alt+${i + 1}`) : undefined, run: (ctx) => { openView(v, ctx.split); } };
    }),
    { id: 'settings.open', title: 'Settings', group: 'Settings', chord: 'Mod+,', run: () => { openView('settings'); } },
    { id: 'find', title: 'Find in panel', group: 'Actions', chord: 'Mod+F', run: () => { findRequest.value++; } },
    { id: 'bg.picker', title: 'Background picker', group: 'Backgrounds', chord: 'Mod+Shift+B', run: () => { popover.value = popover.value === 'bg' ? null : 'bg'; } },
    { id: 'zoom.in', title: 'Zoom in', group: 'Settings', chord: 'Mod+=', run: () => zoomStep(0.1) },
    { id: 'zoom.out', title: 'Zoom out', group: 'Settings', chord: 'Mod+-', run: () => zoomStep(-0.1) },
    { id: 'zoom.reset', title: 'Reset zoom', group: 'Settings', chord: 'Mod+0', run: () => zoomStep(0) },
    { id: 'region.next', title: 'Next region', group: 'Layout', chord: 'F6', run: () => cycleRegion(1) },
    { id: 'region.prev', title: 'Previous region', group: 'Layout', chord: 'Shift+F6', run: () => cycleRegion(-1) },
    { id: 'window.fullscreen', title: 'Toggle full screen', group: 'Layout', chord: 'F11', run: () => { void bridge.window.action('fullscreen'); } },
    { id: 'theme.toggle', title: 'Toggle light/dark theme', group: 'Settings', keywords: ['appearance'],
      run: () => { void patchSettings({ appearance: { theme: document.documentElement.dataset.theme === 'light' ? 'dark' : 'light' } }); } },
    { id: 'web.open', title: 'Open web version of this page', group: 'Actions', keywords: ['serve', 'browser'], run: () => { openWeb('/'); } },
    { id: 'queue.add', title: 'Add tickets…', group: 'Tickets', run: () => hooks.addTickets() },
  ];
  commands.register(c);
}

function toggleBottom(): void {
  const api = getApi();
  if (!api) return;
  const g = api.groups.find((x) => x.api.location.type === 'grid' && x.element.getBoundingClientRect().top > window.innerHeight / 2);
  if (g) { g.api.setVisible(!g.api.isVisible); return; }
  openView('queue', false);
}

const REGIONS = ['.titlebar', '.rail', '.sidebar', '.dock-host', '.statusbar'];
function cycleRegion(d: 1 | -1): void {
  const els = REGIONS.map((s) => document.querySelector<HTMLElement>(s)).filter((x): x is HTMLElement => !!x && x.offsetParent !== null);
  const cur = els.findIndex((x) => x.contains(document.activeElement));
  const n = els[(cur + d + els.length) % els.length];
  const f = n?.querySelector<HTMLElement>('button, [tabindex="0"], input, textarea, .xterm-helper-textarea');
  f?.focus();
}

/** Recompute bindings whenever settings.keymap or the platform changes. */
export function bindKeymap(): void {
  effect(() => {
    const plat = platform.value;
    const defaults: Binding[] = commands.all().filter((x) => x.chord).map((x) => ({ command: x.id, chord: x.chord! }));
    const merged = mergeBindings(defaults, settings.value?.keymap ?? {});
    (keymap as unknown as { platform: string }).platform = plat;
    keymap.set(merged);
  });
  window.addEventListener('keydown', onKeyDown, true);
}

/** Commands from the event, honouring the W18 terminal rule and text-field guard. */
export function commandsFor(e: KeyboardEvent): string[] {
  const t = e.target as HTMLElement | null;
  const inTerm = !!t?.closest?.('.xterm');
  if (inTerm) return keymap.lookupInTerminal(e);
  const ids = keymap.lookup(e);
  if (!ids.length) return ids;
  // plain keys (no modifier) never fire while typing
  if (!e.ctrlKey && !e.metaKey && !e.altKey && !/^F\d+$/.test(e.key) && isTyping(t)) return [];
  return ids;
}

function onKeyDown(e: KeyboardEvent): void {
  if (e.defaultPrevented || e.isComposing) return;
  const ids = commandsFor(e);
  for (const id of ids) {
    const cmd = commands.get(id);
    if (!cmd || (cmd.when && !cmd.when())) continue;
    e.preventDefault();
    e.stopPropagation();
    void commands.run(id, { source: 'key' });
    return;
  }
}

export function recordRecent(id: string): void {
  const r = appState.value?.recents ?? { projects: [], commands: [] };
  patchState({ recents: { ...r, commands: pushRecent(r.commands, id) } });
}
