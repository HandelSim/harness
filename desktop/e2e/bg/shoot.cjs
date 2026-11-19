// Screenshot runner for the bg demo (not a Playwright spec). Usage, from desktop/ after a dev build:
//   xvfb-run -a -s "-screen 0 1280x800x24" node_modules/electron/dist/electron --no-sandbox --use-angle=swiftshader \
//     --enable-unsafe-swiftshader e2e/bg/shoot.cjs $PWD/out/renderer $PWD/docs/progress/bg-shots silk:5000,flow:6000 "&theme=light"
// Each id is id[:waitMs]; the last arg is extra query ("" for dark). Writes <id>[-light].png and report[-light].json.
const { app, BrowserWindow, protocol } = require('electron');
const fs = require('fs'), path = require('path');
const [buildDir, outDir, idsArg, extra = ''] = process.argv.slice(-4);
const CSP = "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'";
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.map': 'application/json' };
protocol.registerSchemesAsPrivileged([{ scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
app.whenReady().then(async () => {
  protocol.handle('app', async (req) => {
    const u = new URL(req.url); const f = path.join(buildDir, decodeURIComponent(u.pathname));
    if (!f.startsWith(buildDir)) return new Response('no', { status: 403 });
    try { return new Response(fs.readFileSync(f), { headers: { 'Content-Type': MIME[path.extname(f)] || 'application/octet-stream', 'Content-Security-Policy': CSP } }); }
    catch { return new Response('nf', { status: 404 }); }
  });
  fs.mkdirSync(outDir, { recursive: true });
  const report = [];
  const win = new BrowserWindow({ width: 1280, height: 800, show: true, backgroundColor: '#0b0c0e', webPreferences: { sandbox: true, contextIsolation: true, backgroundThrottling: false } });
  const logs = [];
  win.webContents.on('console-message', (e) => logs.push(`${e.level}: ${e.message}`));
  for (const id of idsArg.split(',')) {
    logs.length = 0;
    const [bid, wait = '6000'] = id.split(':');
    await win.loadURL(`app://harness/bg-demo.html?id=${bid}&thumbs=0&panel=0${extra}`);
    await sleep(Number(wait));
    const info = await win.webContents.executeJavaScript(`(() => { const r = document.getElementById('bg-root'); return { stats: window.__bg && window.__bg.stats, errors: window.__bg && window.__bg.errors, frames: r && r.dataset.bgFrames, renderer: r && r.dataset.bgRenderer, bgId: r && r.dataset.bgId }; })()`);
    const img = await win.webContents.capturePage();
    const name = bid + (extra.includes('theme=light') ? '-light' : '') + '.png';
    fs.writeFileSync(path.join(outDir, name), img.toPNG());
    report.push({ id: bid, ...info, logs: logs.slice(0, 8) });
  }
  fs.writeFileSync(path.join(outDir, 'report' + (extra.includes('theme=light') ? '-light' : '') + '.json'), JSON.stringify(report, null, 1));
  app.quit();
});
