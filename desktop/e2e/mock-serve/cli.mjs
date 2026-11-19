// Run the mock serve as its own process (manual use; fake-harness imports server.mjs directly).
import { startMock } from './server.mjs';

{
  const arg = (n, d) => { const i = process.argv.indexOf(`--${n}`); return i > 0 ? process.argv[i + 1] : d; };
  const m = await startMock({ port: Number(arg('port', '0')), stateId: arg('state-id', ''), scenario: arg('scenario', 'healthy') });
  process.stdout.write(`[mock-serve] listening on ${m.url}\n`);
  const bye = () => { m.stop().finally(() => process.exit(0)); };
  process.on('SIGTERM', bye); process.on('SIGINT', bye);
}
