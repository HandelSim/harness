// Main window: hardened webPreferences, navigation and window.open denied, bounds persisted.
import { BrowserWindow, screen, shell } from 'electron';
import { join } from 'node:path';
import type { AppState } from '../shared/ipc';
import { APP_PREFIX, externalUrlOk } from './security';
import { emit } from './ipc';
import { log } from './log';

export function createWindow(appPath: string, st: AppState['window'], onBounds: (w: AppState['window']) => void): BrowserWindow {
  const b = fitBounds(st);
  const win = new BrowserWindow({
    ...b, minWidth: 640, minHeight: 420, show: false, backgroundColor: '#0b0d12', title: 'Harness',
    autoHideMenuBar: true,
    webPreferences: {
      preload: join(appPath, 'out/preload/index.cjs'),
      contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, webviewTag: true,
      navigateOnDragDrop: false, spellcheck: false, devTools: true,
    },
  });
  if (st.maximized) win.maximize();
  if (st.fullscreen) win.setFullScreen(true);
  win.once('ready-to-show', () => win.show());
  win.webContents.on('will-navigate', (e, url) => {
    if (url.startsWith(APP_PREFIX) && url.split('#')[0] === win.webContents.getURL().split('#')[0]) return;
    e.preventDefault();
    log('warn', `blocked top-level navigation to ${url.slice(0, 200)}`);
  });
  win.webContents.on('will-redirect', (e) => e.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => {
    log('warn', `blocked window.open ${url.slice(0, 200)}`);
    if (externalUrlOk(url) && !process.env.HARNESS_DESKTOP_E2E) void shell.openExternal(url);
    return { action: 'deny' };
  });
  const save = () => {
    if (win.isDestroyed()) return;
    const n = win.getNormalBounds();
    onBounds({ x: n.x, y: n.y, width: n.width, height: n.height, maximized: win.isMaximized(), fullscreen: win.isFullScreen() });
  };
  const state = () => {
    if (win.isDestroyed()) return;
    emit('window:state', { maximized: win.isMaximized(), fullscreen: win.isFullScreen(), focused: win.isFocused(), visible: win.isVisible() });
  };
  for (const ev of ['resize', 'move', 'maximize', 'unmaximize', 'enter-full-screen', 'leave-full-screen'] as const) win.on(ev as 'resize', save);
  for (const ev of ['focus', 'blur', 'show', 'hide', 'minimize', 'restore', 'maximize', 'unmaximize'] as const) win.on(ev as 'focus', state);
  return win;
}

function fitBounds(st: AppState['window']): { x?: number; y?: number; width: number; height: number } {
  const width = st.width || 1400; const height = st.height || 900;
  if (st.x === undefined || st.y === undefined) return { width, height };
  try {
    const d = screen.getDisplayMatching({ x: st.x, y: st.y, width, height }).workArea;
    const visible = st.x + 80 > d.x && st.y + 40 > d.y && st.x < d.x + d.width - 80 && st.y < d.y + d.height - 40;
    return visible ? { x: st.x, y: st.y, width: Math.min(width, d.width), height: Math.min(height, d.height) } : { width, height };
  } catch { return { width, height }; }
}
