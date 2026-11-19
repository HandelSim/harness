// Classify why an owned `harness serve` failed to come up (1.2 step 5), from its captured output.
export type Failure =
  | { code: 'needs-config'; message: string; missing: string[] }
  | { code: 'key-locked' | 'key-rejected'; message: string; unlockUrl?: string }
  | { code: 'python-missing' | 'port-in-use' | 'host-consent' | 'other'; message: string };

const strip = (s: string) => s.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '');

export function classify(output: string): Failure {
  const t = strip(output);
  const last = t.split('\n').map((l) => l.trim()).filter(Boolean).slice(-3).join(' | ');
  let m = /required value\(s\) empty in \S+: ([A-Z0-9_ ]+)/.exec(t);
  if (m) return { code: 'needs-config', message: 'required settings are empty', missing: m[1].trim().split(/\s+/) };
  if (/missing \S*\.env\b/.test(t)) return { code: 'needs-config', message: '.env is missing', missing: ['.env'] };
  // Wording of the CLI's upstream auth probe (harness: 'ERROR: Upstream API key is locked' + the URL on its own line,
  // 'ERROR: Upstream rejected the API key (HTTP n)').
  if (/Upstream API key is locked/i.test(t)) {
    const url = extractUnlockUrl(t);
    return { code: 'key-locked', message: 'the API key is locked', ...(url ? { unlockUrl: url } : {}) };
  }
  if (/Upstream rejected the API key/i.test(t)) return { code: 'key-rejected', message: 'the API key was rejected' };
  if (/python3?: (command )?not found|No such file or directory: 'python3'|needs python3/i.test(t))
    return { code: 'python-missing', message: 'python3 is not installed' };
  if (/address already in use|port \d+ is (busy|in use|taken)|EADDRINUSE/i.test(t)) return { code: 'port-in-use', message: 'the port is in use' };
  if (/HARNESS_HOST_CONFIRM|host mode.*(confirm|consent)/i.test(t)) return { code: 'host-consent', message: 'host mode needs consent' };
  return { code: 'other', message: last || 'harness serve exited' };
}

export function extractUnlockUrl(t: string): string | undefined {
  // The line after "Visit this URL ... to unlock" holds the URL; else any URL containing "unlock".
  const m = /to unlock[^\n]*\n(?:\s*(?:\[harness\])?\s*\n)*\s*(?:\[harness\])?\s*(https?:\/\/\S+)/i.exec(t)
    || /(https?:\/\/[^\s'"<>)]*unlock[^\s'"<>)]*)/i.exec(t);
  return m ? m[1].replace(/[.,;]+$/, '') : undefined;
}
