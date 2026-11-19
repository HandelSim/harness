// Standalone visual + perf check page for the background engine (dev builds only):
// out/renderer/bg-demo.html. Query: ?id=<bg id>&theme=light&cycle=<seconds>&reduced=1&state=pause&lowpower=off|on|auto
import type { BgConfig, BgStats, BgTheme } from '../shared/bg';
import { createBackground, DEFAULT_BG } from './index';
import { defaultParams, descriptor } from './catalog';

const q = new URLSearchParams(location.search);
const root = document.getElementById('bg-root')!;
const panel = document.getElementById('panel')!;
const thumbs = document.getElementById('thumbs')!;
const engine = createBackground(root, { workerUrl: 'assets/bg-worker.js' });
const themes: Record<string, BgTheme> = {
  dark: { mode: 'dark', base: '#0b0c0e', fg: '#b0b4ba', accent: '#5b5bd6', accent2: '#b1a9ff' },
  light: { mode: 'light', base: '#eceef2', fg: '#1c2024', accent: '#5b5bd6', accent2: '#9b9ef0' },
  teal: { mode: 'dark', base: '#0b0c0e', fg: '#b0b4ba', accent: '#12a594', accent2: '#0bd8b6' },
};
let themeName = q.get('theme') ?? 'dark';
engine.setTheme(themes[themeName] ?? themes.dark);
if (q.get('reduced') === '1') engine.setReducedMotion(true);
// No-GPU policy: ?lowpower=off|on|auto (default: the engine's own default, auto).
const lp = q.get('lowpower');
if (lp === 'off' || lp === 'on' || lp === 'auto') (engine as unknown as { setLowPower(m: string): void }).setLowPower(lp);

const cfgFor = (id: string, over: Partial<BgConfig> = {}): BgConfig =>
  ({ ...DEFAULT_BG, id, params: defaultParams(descriptor(id)!), seed: Number(q.get('seed') ?? 1), ...over });
let cfg = cfgFor(q.get('id') ?? DEFAULT_BG.id);
const errors: string[] = [];
addEventListener('error', (e) => errors.push(String(e.message)));
addEventListener('unhandledrejection', (e) => errors.push(String(e.reason)));

function el<K extends keyof HTMLElementTagNameMap>(tag: K, props: Record<string, unknown> = {}, ...kids: (Node | string)[]): HTMLElementTagNameMap[K] {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'style') n.setAttribute('style', String(v)); else (n as unknown as Record<string, unknown>)[k] = v;
  }
  n.append(...kids);
  return n;
}
const row = (label: string, input: HTMLElement) => el('label', { style: 'display:flex;justify-content:space-between;gap:8px;margin:4px 0' }, label, input);

const sel = el('select');
for (const d of engine.catalog()) sel.append(el('option', { value: d.id, textContent: `${d.group} / ${d.name}` }));
sel.value = cfg.id;
const inten = el('input', { type: 'range', min: '0', max: '1', step: '0.05', value: String(cfg.intensity) });
const speed = el('input', { type: 'range', min: '0', max: '2', step: '0.1', value: String(cfg.speed) });
const seed = el('input', { type: 'number', value: String(cfg.seed), style: 'width:80px' });
const fps = el('select');
for (const f of [15, 24, 30, 60]) fps.append(el('option', { value: String(f), textContent: String(f) }));
fps.value = String(cfg.fpsCap);
const state = el('select');
for (const s of ['run', 'dim', 'pause']) state.append(el('option', { value: s, textContent: s }));
const reduced = el('input', { type: 'checkbox', checked: q.get('reduced') === '1' });
const theme = el('select');
for (const t of Object.keys(themes)) theme.append(el('option', { value: t, textContent: t }));
theme.value = themeName;
const burn = el('input', { type: 'range', min: '0', max: '30', step: '1', value: '0' });
const stats = el('pre', { style: 'margin:8px 0 0;font:11px ui-monospace,monospace;white-space:pre-wrap' });
const cycleBtn = el('button', { textContent: 'Cycle catalog' });
panel.append(el('div', { style: 'font-weight:600;margin-bottom:6px' }, 'Background engine demo'),
  row('Background', sel), row('Intensity', inten), row('Speed', speed), row('Seed', seed), row('FPS cap', fps),
  row('Run state', state), row('Reduced motion', reduced), row('Theme', theme), row('Burn ms/frame', burn), cycleBtn, stats);

const commit = () => engine.set(cfg);
sel.onchange = () => { cfg = cfgFor(sel.value, { intensity: +inten.value, speed: +speed.value, fpsCap: +fps.value as BgConfig['fpsCap'] }); void commit(); };
inten.oninput = () => { cfg = { ...cfg, intensity: +inten.value }; void commit(); };
speed.oninput = () => { cfg = { ...cfg, speed: +speed.value }; void commit(); };
seed.onchange = () => { cfg = { ...cfg, seed: +seed.value >>> 0 }; void commit(); };
fps.onchange = () => { cfg = { ...cfg, fpsCap: +fps.value as BgConfig['fpsCap'] }; void commit(); };
state.onchange = () => engine.setRunState(state.value as 'run');
reduced.onchange = () => engine.setReducedMotion(reduced.checked);
theme.onchange = () => { themeName = theme.value; engine.setTheme(themes[themeName]); void drawThumbs(); };
burn.oninput = () => (engine as unknown as { debugBurn(ms: number): void }).debugBurn(+burn.value);

let last: BgStats | null = null;
engine.onStats((s) => { last = s; });
const paint = () => {
  stats.textContent = last ? `id ${cfg.id}  renderer ${last.renderer}  tier ${last.tier}\nfps ${last.fps}  frame ${last.frameMs} ms  frames ${root.dataset.bgFrames ?? 0}\n${last.downgraded ? `downgraded: ${last.reason}` : ''}${errors.length ? `\nerrors: ${errors.join('; ')}` : ''}` : '';
  setTimeout(paint, 500);
};
paint();

async function drawThumbs(): Promise<void> {
  thumbs.replaceChildren();
  for (const d of engine.catalog()) {
    const img = el('img', { width: 96, height: 60, title: d.name, style: 'border-radius:6px;border:1px solid rgba(255,255,255,0.1);cursor:pointer' });
    img.onclick = () => { sel.value = d.id; sel.onchange?.(new Event('change')); };
    thumbs.append(img);
    try { img.src = await engine.thumbnail(d.id, 96, 60, themes[themeName]); } catch (e) { errors.push(`thumb ${d.id}: ${(e as Error).message}`); }
  }
}

let cycling = 0;
async function cycle(sec: number): Promise<void> {
  if (cycling) { clearTimeout(cycling); cycling = 0; cycleBtn.textContent = 'Cycle catalog'; return; }
  const ids = engine.catalog().map((d) => d.id);
  let i = Math.max(0, ids.indexOf(cfg.id));
  cycleBtn.textContent = 'Stop cycling';
  const next = () => { i = (i + 1) % ids.length; sel.value = ids[i]; sel.onchange?.(new Event('change')); cycling = setTimeout(next, sec * 1000) as unknown as number; };
  cycling = setTimeout(next, sec * 1000) as unknown as number;
}
cycleBtn.onclick = () => void cycle(Number(q.get('cycle')) || 4);

// Hooks for e2e and screenshots.
(window as unknown as Record<string, unknown>).__bg = { engine, errors, get stats() { return last; }, set: (c: Partial<BgConfig> & { id: string }) => { cfg = cfgFor(c.id, c); sel.value = c.id; return engine.set(cfg); } };
void commit().then(() => { document.documentElement.dataset.ready = '1'; });
if (q.get('state')) engine.setRunState(q.get('state') as 'run');
if (q.get('thumbs') !== '0') void drawThumbs();
if (q.get('cycle')) void cycle(Number(q.get('cycle')));
