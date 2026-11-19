// Sandboxed preload: exposes exactly one object, window.harness (Bridge). No ipcRenderer leaks.
import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron';
import type { Bridge, BridgeEvents } from '../shared/ipc';

const EVENTS: (keyof BridgeEvents)[] = ['backend:state', 'live:update', 'live:health', 'job:output', 'job:exit',
  'keys:changed', 'settings:changed', 'pty:data', 'pty:exit', 'update:state', 'window:state', 'power:state',
  'notify:click', 'menu:command', 'app:will-quit'];

const inv = (ch: string, payload?: unknown) => ipcRenderer.invoke(ch, payload);
const send = (ch: string, payload: unknown) => ipcRenderer.send(ch, payload);

const bridge: Bridge = {
  on(event, cb) {
    if (!EVENTS.includes(event)) return () => {};
    const fn = (_e: IpcRendererEvent, payload: unknown) => { try { cb(payload as never); } catch (err) { console.error(err); } };
    ipcRenderer.on(event, fn);
    return () => { ipcRenderer.removeListener(event, fn); };
  },
  app: { info: () => inv('app:info') },
  backend: { get: () => inv('backend:get'), action: (a) => inv('backend:action', a) },
  api: {
    get: (path, query, sig) => inv('api:get', { path, query, sig }),
    post: (path, body) => inv('api:post', { path, body: body ?? {} }),
    bytes: (path) => inv('api:bytes', { path }),
  },
  live: { set: (topics) => inv('live:set', { topics }) },
  harness: {
    configList: () => inv('harness:config-list'),
    configSet: (key, value) => inv('harness:config-set', { key, value }),
    job: (job) => inv('harness:job', { job }),
    updateGet: () => inv('harness:update-get'),
  },
  keys: {
    list: () => inv('keys:list'),
    add: (k) => inv('keys:add', k),
    update: (k) => inv('keys:update', k),
    remove: (id) => inv('keys:remove', { id }),
    activate: (id) => inv('keys:activate', { id }),
    probe: (target) => inv('keys:probe', target),
    clearActive: (kind) => inv('keys:clear-active', { kind }),
  },
  settings: { get: () => inv('settings:get'), set: (patch) => inv('settings:set', { patch }) },
  state: { get: () => inv('state:get'), set: (patch) => inv('state:set', { patch }) },
  pty: {
    spawn: (spec, cols, rows) => inv('pty:spawn', { spec, cols, rows }),
    write: (id, data) => send('pty:write', { id, data }),
    resize: (id, cols, rows) => send('pty:resize', { id, cols, rows }),
    ack: (id, chars) => send('pty:ack', { id, chars }),
    kill: (id) => send('pty:kill', { id }),
    list: () => inv('pty:list'),
    attach: (id) => inv('pty:attach', { id }),
  },
  web: { url: (target, path) => inv('web:url', { target, path }), theme: (css) => inv('web:theme', { css }) },
  update: { get: () => inv('update:get'), check: () => inv('update:check'), download: () => inv('update:download'),
    install: () => inv('update:install') },
  shell: {
    openExternal: (url) => inv('shell:open-external', { url }),
    showItem: (path) => inv('shell:show-item', { path }),
    pickFolder: (defaultPath) => inv('shell:pick-folder', { defaultPath }),
    pathInfo: (path, list) => inv('shell:path-info', { path, list }),
    saveFile: (name, text) => inv('shell:save-file', { name, text }),
  },
  window: { action: (action, value) => inv('window:action', { action, value }) },
  notify: { show: (n) => inv('notify:show', n) },
  log: (level, msg) => send('log:write', { level, msg }),
};

contextBridge.exposeInMainWorld('harness', bridge);
