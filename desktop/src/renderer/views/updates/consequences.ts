// W15 / H2 / M3: what Restart-to-update and Upgrade interrupt, as the confirm dialogs list it. Pure, unit-tested.
import type { AppBusy } from '../../../shared/ipc';

export function restartConsequences(b: AppBusy | null): string[] {
  if (!b) return ['Could not check for open terminals or a running queue.'];
  const out: string[] = [];
  if (b.terminals) out.push(`${b.terminals} open terminal${b.terminals === 1 ? ' is' : 's are'} closed (agent sessions in them end).`);
  if (b.queueRunning) out.push(b.ownedServe && b.onQuit === 'stop'
    ? 'A queue run is active, and the app stops the backend it started when it quits (Settings: on quit = stop).'
    : 'A queue run is active; the backend keeps running while the app restarts.');
  if (b.jobs.length) out.push(`A harness job is still running (${b.jobs.join(', ')}); restarting now may interrupt it.`);
  if (!out.length) out.push('Nothing is running that the restart would interrupt.');
  return out;
}
export function upgradeConsequences(b: AppBusy | null): string[] {
  const out = ['harness serve restarts itself; Web panels reload and a web chat in flight fails.'];
  if (!b) out.push('Could not check whether a queue run is active.');
  else if (b.queueRunning) out.push('A queue run is active: it restarts between tickets.');
  return out;
}

