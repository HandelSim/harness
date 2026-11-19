// Native application menu: items dispatch into the renderer's command registry (menu:command).
import { Menu, type MenuItemConstructorOptions } from 'electron';

export function buildMenu(send: (command: string) => void): void {
  const c = (label: string, command: string, accelerator?: string): MenuItemConstructorOptions =>
    ({ label, accelerator, click: () => send(command) });
  const mac = process.platform === 'darwin';
  const tpl: MenuItemConstructorOptions[] = [
    ...(mac ? [{ role: 'appMenu' as const }] : []),
    { label: 'File', submenu: [c('New session…', 'session.new'), c('Command palette', 'palette.open'), { type: 'separator' },
      c('Settings', 'view.settings'), { type: 'separator' }, mac ? { role: 'close' } : { role: 'quit' }] },
    { role: 'editMenu' },
    { label: 'View', submenu: [c('Home', 'view.home'), c('Queue', 'view.queue'), c('Sessions', 'view.sessions'), { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { type: 'separator' }, { role: 'togglefullscreen' }, { role: 'toggleDevTools' }] },
    { role: 'windowMenu' },
    { label: 'Help', submenu: [c('Keyboard shortcuts', 'help.shortcuts'), c('Check for updates', 'updates.check')] },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(tpl));
}
