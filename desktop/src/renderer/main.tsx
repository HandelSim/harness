// Renderer entry. All CSS is imported here (esbuild does not load CSS of lazy chunks).
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '@xterm/xterm/css/xterm.css';
import './styles/dockview-base.css';
import './styles/tokens.css';
import './styles/base.css';
import './styles/glass.css';
import './styles/dockview.css';
import './styles/shell.css';
import './styles/views.css';
import './views/queue/queue.css';
import './views/ticket/ticket.css';
import './views/terminal/terminal.css';
import './views/session/session.css';
import './views/settings/settings.css';
import './views/keys/keys.css';
import './views/transcript/transcript.css';
import { render } from 'preact';
import { App } from './shell/App';
import { bindAppearance, startApp } from './core/app';
import { registerDefaultCommands, bindKeymap, commands } from './shell/commands';
import { registerPanels } from './shell/registry';
import { startBackground } from './core/background';
import { on, bridge } from './core/bridge';
import { flushState, settings, store, updateState, windowState } from './core/app';
import { saveLayoutNow, openPanel, openView } from './shell/dock';
import { startNotify, parseRoute } from './core/notify';
import { VIEW_IDS, type ViewId } from './shell/panels';

bindAppearance();
registerPanels();
registerDefaultCommands();
bindKeymap();
on('menu:command', ({ command }) => { void commands.run(command, { source: 'menu' }); });
// L5: flush, then ack so main closes now instead of after its 1.5 s fallback (IPC is ordered: the state write lands first)
on('app:will-quit', () => { saveLayoutNow(); flushState(); void bridge.window.action('close').catch(() => {}); });

// W22 / M4: OS notifications while the window is not focused. The queue topic is kept subscribed only while a
// queue notification kind is on, so main keeps polling it (at the blurred / hidden cadence).
let unsubQueue: (() => void) | null = null;
settings.subscribe((s) => {
  const n = s?.notifications;
  const want = !!n && (n.needsInput || n.ticketDone || n.runEnded);
  queueMicrotask(() => {
    if (want && !unsubQueue) unsubQueue = store.subscribe('queue');
    else if (!want && unsubQueue) { unsubQueue(); unsubQueue = null; }
  });
});
startNotify({ queue: store.entry('queue').data, update: updateState, settings,
  focused: () => windowState.value.focused && windowState.value.visible, show: (n) => bridge.notify.show(n) });
on('notify:click', ({ route }) => {
  const r = parseRoute(route);
  if (r?.kind === 'ticket') openPanel('ticket', r.key, { title: r.key, params: { key: r.key } });
  else if (r?.kind === 'view' && (VIEW_IDS as readonly string[]).includes(r.view)) openView(r.view as ViewId);
});
window.addEventListener('beforeunload', () => { saveLayoutNow(); flushState(); });
const bgRoot = document.getElementById('bg-root');
if (bgRoot) void startBackground(bgRoot);
void Promise.race([startApp(), new Promise((r) => setTimeout(r, 1500))]).finally(() => {
  render(<App />, document.getElementById('app')!);
});
