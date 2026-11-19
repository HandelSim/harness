// New session dialog (W2/W3): folder (recents + completion + Browse, live path check with git branch), mode cards,
// agent options, host consent, open-in. Mod+Shift+T ("again") reuses the last spec without a dialog.
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import type { PtySpec, PathInfo } from '../../../shared/ipc';
import { openDialog, closeDialog } from '../../shell/ui';
import { openPanel } from '../../shell/dock';
import { settings, appState, patchState, backend } from '../../core/app';
import { bridge } from '../../core/bridge';
import { fail } from '../../core/toast';
import { Button, Seg } from '../../ui/kit';
import { Icon } from '../../ui/Icon';
import { specTitle, baseName } from '../terminal/model';
import { KINDS, isAgent, defaultForm, formFromSpec, buildSpec, validate, pushRecentProject, completions, dirName, normalizePath, gitCase, type SessionForm, type OpenIn, type Kind } from './model';

const LAST_KEY = 'session.last';
let last: { spec: PtySpec; openIn: OpenIn } | null = null;

function lastChoice(): { spec: PtySpec; openIn: OpenIn } | null {
  if (last) return last;
  const v = appState.peek()?.panels?.[LAST_KEY] as { spec?: PtySpec; openIn?: OpenIn } | undefined;
  if (v && v.spec && typeof v.spec.cwd === 'string' && KINDS.some((k) => k.value === v.spec!.kind)) {
    last = { spec: v.spec, openIn: v.openIn ?? 'here' };
  }
  return last;
}

function launch(spec: PtySpec, openIn: OpenIn): void {
  last = { spec, openIn };
  const st = appState.peek();
  if (st) {
    patchState({
      panels: { ...(st.panels ?? {}), [LAST_KEY]: last },
      recents: { ...st.recents, projects: pushRecentProject(st.recents?.projects ?? [], spec.cwd) },
    });
  }
  openPanel('terminal', null, { title: specTitle(spec), params: { spec }, split: openIn === 'here' ? undefined : openIn });
}

/** Mod+T opens the dialog (remembering the last choice); again=true (Mod+Shift+T) relaunches it without one. */
export function openNewSession(again = false): void {
  const l = lastChoice();
  if (again && l) {
    // the host consent may have been revoked since
    if (l.spec.kind === 'host' && backend.peek() && !backend.peek()!.hostConsent) { openDialog(<NewSessionDialog initial={l.spec} />); return; }
    launch(l.spec, l.openIn);
    return;
  }
  openDialog(<NewSessionDialog initial={l?.spec ?? null} openIn={l?.openIn} />);
}
/** Open the dialog pre-filled (terminal "Edit options" / "Try host mode", folder drop). */
export function openNewSessionWith(spec: Partial<PtySpec> & { cwd: string }): void {
  const base = lastChoice()?.spec;
  openDialog(<NewSessionDialog initial={{ ...(base ?? { kind: 'agent' as Kind }), ...spec } as PtySpec} />);
}

function NewSessionDialog({ initial, openIn }: { initial: PtySpec | null; openIn?: OpenIn }) {
  const recents = appState.peek()?.recents?.projects ?? [];
  const [form, setForm] = useState<SessionForm>(() => {
    if (initial) return formFromSpec(initial, openIn ?? 'here');
    return defaultForm(settings.peek()?.sessions.defaultKind ?? 'agent', recents[0] ?? '');
  });
  const [info, setInfo] = useState<PathInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [entries, setEntries] = useState<string[]>([]);
  const [consent, setConsent] = useState(false);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const folderRef = useRef<HTMLInputElement>(null);
  const seq = useRef(0);
  const hostConsent = backend.value?.hostConsent ?? false;
  const set = <K extends keyof SessionForm>(k: K, v: SessionForm[K]) => setForm((f) => ({ ...f, [k]: v }));

  // live folder check (debounced, stale replies dropped)
  useEffect(() => {
    const p = normalizePath(form.cwd);
    const n = ++seq.current;
    setInfo(null);
    if (!p) { setChecking(false); return; }
    setChecking(true);
    const tm = setTimeout(async () => {
      const parent = /[\\/]$/.test(form.cwd.trim()) ? p : dirName(p);
      const [self, par] = await Promise.all([
        bridge.shell.pathInfo(p).catch(() => null),
        parent ? bridge.shell.pathInfo(parent, true).catch(() => null) : Promise.resolve(null),
      ]);
      if (n !== seq.current) return;
      setInfo(self ?? { exists: false, dir: false, git: null });
      setEntries(par?.entries ?? []);
      setChecking(false);
    }, 140);
    return () => clearTimeout(tm);
  }, [form.cwd]);

  useEffect(() => { requestAnimationFrame(() => { const el = folderRef.current; if (el) { el.focus(); el.select(); } }); }, []);

  const errors = validate(form, checking ? null : info, hostConsent, consent);
  const canSubmit = !busy && !checking && !!info && Object.keys(errors).length === 0;
  const sugg = useMemo(() => completions(form.cwd, entries, recents), [form.cwd, entries, recents]);
  const kindMeta = KINDS.find((k) => k.value === form.kind)!;

  async function submit() {
    setTouched(true);
    if (!canSubmit) return;
    setBusy(true);
    try {
      if (form.kind === 'host' && !hostConsent) {
        const st = await bridge.backend.action({ action: 'consent-host' });
        backend.value = st;
        if (!st.hostConsent) { fail('Host consent was not recorded'); return; }
      }
      const spec = buildSpec(form);
      closeDialog();
      launch(spec, form.openIn);
    } catch (e) {
      fail('Could not start the session', String((e as Error)?.message ?? e));
    } finally { setBusy(false); }
  }

  async function browse() {
    const p = await bridge.shell.pickFolder(normalizePath(form.cwd) || recents[0] || undefined).catch(() => null);
    if (p) { set('cwd', p); folderRef.current?.focus(); }
  }

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    const t = e.target as HTMLElement;
    if (t.tagName === 'BUTTON' || t.tagName === 'TEXTAREA') return;
    e.preventDefault();
    void submit();
  };

  const folderMsg = (() => {
    if (checking) return <span class="ns-path faint"><span class="ns-dot ns-dot--wait" />Checking…</span>;
    if (errors.cwd && (touched || form.cwd)) return <span class="ns-path ns-path--err" data-testid="ns-folder-error"><Icon name="alert" size={12} />{errors.cwd}</span>;
    if (info?.dir) {
      const g = gitCase(info);
      return (
        <span class={`ns-path${g.tone === 'warn' ? ' ns-path--warn' : ''}`} data-testid="ns-folder-ok" data-git={g.repo ? 'repo' : 'none'} title={g.text}>
          <Icon name={g.tone === 'warn' ? 'alert' : 'check'} size={12} />
          <span class="ellipsis">{g.text}</span>
          {g.root ? <button type="button" class="linkish" data-testid="ns-use-root" onClick={() => set('cwd', g.root!)}>use the repo's top folder</button> : null}
        </span>
      );
    }
    return null;
  })();

  return (
    <div class="dialog glass glass--overlay ns" data-testid="dialog-new-session" role="dialog" aria-modal="true" aria-labelledby="ns-title" onKeyDown={onKeyDown}>
      <header><Icon name="terminal" size={16} /><span id="ns-title">New session</span></header>
      <div class="dialog-body">
        <div class="field">
          <label for="ns-folder">Folder</label>
          <div class="ns-folder-row">
            <input id="ns-folder" ref={folderRef} class="input mono grow" list="ns-folder-list" spellcheck={false} autocomplete="off"
              placeholder="/path/to/project" value={form.cwd} data-testid="ns-folder"
              aria-invalid={!!errors.cwd && touched} onInput={(e) => set('cwd', (e.currentTarget as HTMLInputElement).value)} />
            <datalist id="ns-folder-list">{sugg.map((s) => <option key={s} value={s} />)}</datalist>
            <Button icon="folder" onClick={browse} testid="ns-browse">Browse…</Button>
          </div>
          <div class="ns-path-line">{folderMsg}</div>
          {recents.length ? (
            <div class="ns-recents" role="group" aria-label="Recent projects">
              {recents.slice(0, 6).map((r) => (
                <button key={r} type="button" class={`ns-recent ${normalizePath(r) === normalizePath(form.cwd) ? 'is-on' : ''}`} title={r}
                  onClick={() => set('cwd', r)} data-testid={`ns-recent-${baseName(r)}`}>
                  <Icon name="folder" size={12} /><span class="ellipsis">{baseName(r)}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>

        <div class="field">
          <label>Mode</label>
          <div class="ns-kinds" role="radiogroup" aria-label="Mode" data-testid="ns-kind">
            {KINDS.map((k) => (
              <button key={k.value} type="button" role="radio" aria-checked={form.kind === k.value} class={`ns-kind ${form.kind === k.value ? 'is-on' : ''}`}
                onClick={() => set('kind', k.value)} data-testid={`ns-kind-${k.value}`}>
                <span class="ns-kind__label">{k.label}</span>
                <span class="ns-kind__cmd mono">{k.cmd}</span>
              </button>
            ))}
          </div>
          <span class="hint">{kindMeta.blurb}</span>
        </div>

        {isAgent(form.kind) ? (
          <div class="field">
            <label>Options</label>
            <div class="ns-opts">
              <label class="check"><input type="checkbox" checked={form.yolo} onChange={(e) => set('yolo', (e.currentTarget as HTMLInputElement).checked)} data-testid="ns-yolo" />YOLO <span class="faint">auto-approve</span></label>
              <label class="check"><input type="checkbox" checked={form.normal} onChange={(e) => set('normal', (e.currentTarget as HTMLInputElement).checked)} data-testid="ns-normal" />Normal prompts</label>
              <label class="check"><input type="checkbox" checked={form.thoughts} onChange={(e) => set('thoughts', (e.currentTarget as HTMLInputElement).checked)} data-testid="ns-thoughts" />Thoughts</label>
              <label class="check"><input type="checkbox" checked={form.net} onChange={(e) => set('net', (e.currentTarget as HTMLInputElement).checked)} data-testid="ns-net" />Network</label>
              {form.kind === 'host' ? (
                <label class="check"><input type="checkbox" checked={form.resume} onChange={(e) => set('resume', (e.currentTarget as HTMLInputElement).checked)} data-testid="ns-resume" />Resume last session</label>
              ) : null}
            </div>
            <div class="ns-ticket">
              <input class="input mono" placeholder="Ticket key (optional), e.g. AB-123" value={form.ticket} spellcheck={false} autocomplete="off" aria-label="Ticket key"
                aria-invalid={!!errors.ticket} onInput={(e) => set('ticket', (e.currentTarget as HTMLInputElement).value)} data-testid="ns-ticket" />
              {errors.ticket ? <span class="err">{errors.ticket}</span> : null}
            </div>
          </div>
        ) : null}

        {form.kind === 'host' && !hostConsent ? (
          <div class="notice notice--warn ns-consent" data-testid="ns-host-consent">
            <Icon name="alert" size={14} />
            <div class="col">
              <span>Host mode runs the agent directly on this machine with your user's permissions: no container, no firewall.</span>
              <label class="check"><input type="checkbox" checked={consent} onChange={(e) => setConsent((e.currentTarget as HTMLInputElement).checked)} data-testid="ns-consent" />I understand, allow host mode</label>
            </div>
          </div>
        ) : null}

        <div class="field">
          <label>Open in</label>
          <Seg<OpenIn> label="Open in" value={form.openIn} onChange={(v) => set('openIn', v)}
            options={[{ value: 'here', label: 'This group' }, { value: 'right', label: 'Split right' }, { value: 'down', label: 'Split down' }]} />
        </div>
      </div>
      <footer>
        <span class="faint small ns-keys"><kbd>Enter</kbd> start · <kbd>Esc</kbd> cancel</span>
        <span class="spacer" />
        <Button variant="ghost" onClick={() => closeDialog()} testid="ns-cancel">Cancel</Button>
        <Button variant="primary" icon="play" onClick={submit} disabled={!canSubmit} testid="ns-start"
          title={!canSubmit ? (errors.cwd ?? errors.consent ?? errors.ticket ?? (checking ? 'Checking folder…' : '')) : undefined}>Start</Button>
      </footer>
    </div>
  );
}
