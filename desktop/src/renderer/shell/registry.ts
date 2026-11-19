// Lazy panel registry: dockview component name -> dynamic import. Views not listed fall back to the Web panel.
import { registerPanel, type PanelComponent } from './dock';
import { hooks } from './commands';

const lazy = (f: () => Promise<{ default: PanelComponent }>) => () => f().then((m) => m.default);

export function registerPanels(): void {
  registerPanel('web', lazy(() => import('../views/web/WebPanel')));
  registerPanel('view.queue', lazy(() => import('../views/queue/QueueView')));
  registerPanel('ticket', lazy(() => import('../views/ticket/TicketPanel')));
  registerPanel('view.settings', lazy(() => import('../views/settings/SettingsView')));
  registerPanel('view.keys', lazy(() => import('../views/keys/KeysView')));
  registerPanel('view.updates', lazy(() => import('../views/updates/UpdatesView')));
  registerPanel('view.sessions', lazy(() => import('../views/sessions/SessionsView')));
  registerPanel('transcript', lazy(() => import('../views/transcript/TranscriptPanel')));
  registerPanel('terminal', lazy(() => import('../views/terminal/TerminalPanel')));
  registerPanel('view.vault', lazy(() => import('../views/vault/VaultView')));
  registerPanel('vault-file', lazy(() => import('../views/vault/VaultFilePanel')));
  registerPanel('view.jira', lazy(() => import('../views/jira/JiraView')));
  registerPanel('view.debug', lazy(() => import('../views/debug/DebugView')));
  registerPanel('diff', lazy(() => import('../views/ticket/DiffView')));
  registerPanel('view.home', lazy(() => import('../views/home/HomeView')));
  registerPanel('view.findings', lazy(() => import('../views/findings/FindingsView')));
  registerPanel('chat', lazy(() => import('../views/chat/ChatPanel')));
  hooks.newSession = (again) => { void import('../views/session/NewSessionDialog').then((m) => m.openNewSession(again)); };
  hooks.addTickets = () => { void import('../views/queue/AddTicketsDialog').then((m) => m.openAddTickets()); };
}
