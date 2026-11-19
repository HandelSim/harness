// Add tickets dialog (W5): Jira keys or a ticket of your own, project, starting point, stages, after-plan, note.
// The server validates everything; a refusal is shown inline and every field is kept.
// W5 step 3-4 / F1 / F3: the project folder is probed (exists, git case); its branches and repositories load from
// /queue/branches and /queue/repos, an existing branch named after the ticket preselects Continue, and a project with
// several repositories gets a branch mode per repository.
import { useEffect, useRef, useState } from 'preact/hooks';
import { act, appState, patchState, offlineReason } from '../../core/app';
import { bridge, api } from '../../core/bridge';
import { gitCase, ticketBranch, normalizePath, isAbsolute } from '../session/model';
import type { PathInfo } from '../../../shared/ipc';
import { openDialog, closeDialog } from '../../shell/ui';
import { Button, Field, Seg } from '../../ui/kit';
import { Icon } from '../../ui/Icon';

type Kind = 'jira' | 'local';
type RepoMode = 'branch' | 'uncommitted';
/** GET /queue/branches (runs.project_branches) */
interface Branches { top?: string; current?: string; default?: string; local?: string[]; remote?: string[]; remotes?: string[]; defaults?: string[]; error?: string }
/** GET /queue/repos (runs.repo_tree) */
interface RepoRow { path: string; depth: number; branch: string | null; head: string | null; dirty: number; ahead: number | null; behind: number | null; branches: string[]; err?: string }
interface Repos { project?: string; repos?: RepoRow[]; more?: number; warn?: string; error?: string }
const data = <T,>(r: { ok: boolean; data?: unknown } | null): T | null => (r && r.ok && r.data && typeof r.data === 'object' ? r.data as T : null);
type Start = '' | 'new' | 'continue' | 'here';
type Preset = 'full' | 'tests' | 'implement' | 'review' | 'plan' | 'custom';
const STAGES = ['plan', 'test', 'implement', 'review', 'fix', 'push'] as const;
const PRESETS: { value: Preset; label: string; hint: string }[] = [
  { value: 'full', label: 'Full', hint: 'plan, implement, review with fix rounds, push' },
  { value: 'tests', label: 'Full + tests', hint: 'the same, with tests written from the plan before the code' },
  { value: 'implement', label: 'Implement', hint: 'implement, review, fix, push (no plan)' },
  { value: 'review', label: 'Review only', hint: 'review work that already exists; nothing is pushed' },
  { value: 'plan', label: 'Plan only', hint: 'write the plan and stop' },
  { value: 'custom', label: 'Custom', hint: 'pick the stages' },
];

function AddTickets({ initialKeys = '' }: { initialKeys?: string }) {
  const recents = appState.value?.recents?.projects ?? [];
  const [kind, setKind] = useState<Kind>('jira');
  const [keys, setKeys] = useState(initialKeys);
  const [title, setTitle] = useState('');
  const [desc, setDesc] = useState('');
  const [project, setProject] = useState(recents[0] ?? '');
  const [start, setStart] = useState<Start>('');
  const [cont, setCont] = useState('');
  const [base, setBase] = useState('');     // web parity: Target branch (serve/runs.py add form `base`)
  const [branch, setBranch] = useState(''); // web parity: Work branch name (`branch`)
  const [preset, setPreset] = useState<Preset>('full');
  const [stages, setStages] = useState<string[]>(['plan', 'implement', 'review', 'fix', 'push']);
  const [after, setAfter] = useState<'wait' | 'go' | 'defaults'>('wait');
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const off = offlineReason.value;
  const [info, setInfo] = useState<PathInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [br, setBr] = useState<Branches | null>(null);
  const [repos, setRepos] = useState<Repos | null>(null);
  const [modes, setModes] = useState<Record<string, RepoMode>>({});
  const seq = useRef(0);
  const startTouched = useRef(false);
  const autoCont = useRef('');

  // project probe (debounced; stale replies dropped): path-info, then the server's branches and repositories
  useEffect(() => {
    const p = normalizePath(project);
    const n = ++seq.current;
    setInfo(null); setBr(null); setRepos(null); setModes({});
    if (!p || !isAbsolute(p)) { setChecking(false); return; }
    setChecking(true);
    const tm = setTimeout(async () => {
      const i = await bridge.shell.pathInfo(p).catch(() => null);
      if (n !== seq.current) return;
      setInfo(i ?? { exists: false, dir: false, git: null }); setChecking(false);
      if (!i?.dir || !gitCase(i).repo || offlineReason.peek()) return;
      const [b, r] = await Promise.all([
        bridge.api.get<Branches>(api('/queue/branches'), { project: p }).catch(() => null),
        bridge.api.get<Repos>(api('/queue/repos'), { project: p }).catch(() => null)]);
      if (n !== seq.current) return;
      setBr(data<Branches>(b));
      const rd = data<Repos>(r);
      setRepos(rd);
      if (rd?.repos) setModes(Object.fromEntries(rd.repos.map((x) => [x.path, x.path === '.' ? 'branch' : 'uncommitted'] as const)));
    }, 250);
    return () => clearTimeout(tm);
  }, [project]);

  // a branch already named after the (single) ticket: preselect Continue, as the web form does
  const words = kind === 'jira' ? keys.trim().split(/[\s,]+/).filter(Boolean) : [];
  const oneKey = words.length === 1 ? words[0] : '';
  const existing = br && !br.error && oneKey ? (ticketBranch(oneKey, br.local || [], br.remote || []) || ticketBranch(oneKey.toUpperCase(), br.local || [], br.remote || [])) : '';
  useEffect(() => {
    if (startTouched.current) return;
    if (existing) { autoCont.current = existing; setStart('continue'); setCont(existing); }
    else if (autoCont.current) { autoCont.current = ''; setStart(''); setCont(''); }
  }, [existing]);
  const pickStart = (v: Start) => { startTouched.current = true; setStart(v); };

  const pth = normalizePath(project);
  const projErr = !pth ? '' : !isAbsolute(pth) ? 'Use an absolute path' : checking || !info ? '' : !info.exists ? 'Folder does not exist' : !info.dir ? 'Not a folder' : '';
  const gc = info?.dir ? gitCase(info) : null;
  const tree = repos?.repos && repos.repos.length > 1 ? repos.repos : null;
  const branchNames = br && !br.error ? [...(br.local || []), ...(br.remote || [])] : [];

  const submit = async () => {
    setErr('');
    if (projErr) { setErr(`Project: ${projErr}`); return; }
    const body: Record<string, unknown> = { kind, project, preset, after_plan: after };
    if (kind === 'jira') body.keys = keys; else { body.title = title; body.desc = desc; }
    if (start) body.start = start;
    if (start === 'continue') body.cont = cont;
    if (base.trim()) body.base = base.trim();
    if (branch.trim()) body.branch = branch.trim();
    if (preset === 'custom') body.stages = stages;
    if (tree) body.repo_modes = modes;
    if (note.trim()) body.note = note;
    const r = await act('/queue/add', body, { refresh: ['queue'] });
    if (!r.ok) { setErr(r.error.message || 'The server refused the tickets.'); return; }
    if (project) {
      const p = [project, ...recents.filter((x) => x !== project)].slice(0, 10);
      patchState({ recents: { projects: p, commands: appState.value?.recents?.commands ?? [] } });
    }
    closeDialog();
  };
  const pick = async () => {
    const r = await bridge.shell.pickFolder(project || undefined);
    if (r) setProject(typeof r === 'string' ? r : String((r as { path?: string }).path ?? ''));
  };
  const toggleStage = (s: string) => setStages(stages.includes(s) ? stages.filter((x) => x !== s) : STAGES.filter((x) => x === s || stages.includes(x)));
  return (
    <div class="dialog dialog--wide glass glass--overlay" role="dialog" aria-label="Add tickets" data-testid="dialog-add-tickets"
      onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void submit(); } }}>
      <header><Icon name="plus" />Add tickets<span class="spacer" />
        <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Close" onClick={closeDialog} /></header>
      <div class="dialog-body col" style={{ gap: 'var(--sp-4)' }}>
        <Seg label="Kind of ticket" value={kind} onChange={setKind} options={[{ value: 'jira', label: 'Jira tickets' }, { value: 'local', label: 'A ticket of your own' }]} />
        {kind === 'jira' ? (
          <Field label="Ticket keys" hint="One or more keys, separated by spaces or commas (like ABC-123).">
            <input class="input mono" data-testid="add-keys" value={keys} autoFocus onInput={(e) => setKeys((e.target as HTMLInputElement).value)} placeholder="ABC-123 ABC-124" />
          </Field>
        ) : (
          <>
            <Field label="Title"><input class="input" data-testid="add-title" value={title} autoFocus onInput={(e) => setTitle((e.target as HTMLInputElement).value)} /></Field>
            <Field label="Description" hint="Saved in the active vault with the ticket."><textarea class="textarea" rows={4} value={desc} onInput={(e) => setDesc((e.target as HTMLTextAreaElement).value)} /></Field>
          </>
        )}
        <Field label="Project" hint="The folder the agent works in. Empty: the queue's default.">
          <div class="row">
            <input class="input mono grow" data-testid="add-project" list="add-recent-projects" value={project} onInput={(e) => setProject((e.target as HTMLInputElement).value)} placeholder="/path/to/project" />
            <datalist id="add-recent-projects">{recents.map((p) => <option key={p} value={p} />)}</datalist>
            <Button size="sm" icon="folder" onClick={pick}>Choose…</Button>
          </div>
          <div class="meta small" data-testid="add-project-info" data-case={projErr ? 'error' : gc ? (gc.repo ? (gc.root ? 'subfolder' : gc.tone === 'warn' ? 'warn' : 'repo') : 'not-git') : ''} style={{ minHeight: '1.2em' }}>
            {checking ? 'Checking…' : projErr ? <span class="key-warn" role="alert">{projErr}</span> : gc ? (
              <>
                <span style={{ color: gc.tone === 'warn' ? 'var(--warn-text)' : undefined }}>{gc.text}</span>
                {br?.default ? <span> · origin default {br.default}</span> : null}
                {gc.root ? <> · <a href="#" data-testid="add-use-root" onClick={(e) => { e.preventDefault(); setProject(gc.root!); }}>use the repo's top folder</a></> : null}
              </>
            ) : null}
          </div>
          {existing ? (
            <div class="notice" data-testid="add-existing-branch"><Icon name="info" />
              A branch named after the ticket exists: <span class="mono">{existing}</span>. {start === 'continue' && cont === existing ? 'Continue existing work is selected; pick New branch to start fresh.' : ''}
            </div>
          ) : null}
        </Field>
        <Field label="Starting point">
          <Seg label="Starting point" value={start} onChange={pickStart} options={[{ value: '', label: 'Default' }, { value: 'new', label: 'New branch' }, { value: 'continue', label: 'Continue existing work' }, { value: 'here', label: 'Here, as it is' }]} />
        </Field>
        {start === 'continue' ? (
          <Field label="Continue from" hint="The branch (or commit) the work is on.">
            <input class="input mono" data-testid="add-cont" list="add-branches" value={cont} onInput={(e) => setCont((e.target as HTMLInputElement).value)} />
            <datalist id="add-branches">{branchNames.map((b) => <option key={b} value={b} />)}</datalist>
          </Field>
        ) : null}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(200px, 100%), 1fr))', gap: 'var(--sp-3)' }}>
          <Field label="Target branch" hint="Where the work lands. Empty: the branch the folder is on.">
            <input class="input mono" data-testid="add-base" list="add-base-branches" value={base} spellcheck={false} placeholder="main"
              onInput={(e) => setBase((e.target as HTMLInputElement).value)} />
            <datalist id="add-base-branches">{branchNames.map((b) => <option key={b} value={b} />)}</datalist>
          </Field>
          <Field label="Work branch name" hint="Empty: named after the ticket.">
            <input class="input mono" data-testid="add-branch" value={branch} spellcheck={false} onInput={(e) => setBranch((e.target as HTMLInputElement).value)} />
          </Field>
        </div>
        {tree ? (
          <Field label="Repositories" hint="How each repository's changes land: on a work branch, or left uncommitted in the folder.">
            <div class="col" data-testid="add-repos" style={{ gap: 'var(--sp-1)' }}>
              {tree.map((r) => (
                <div key={r.path} class="row" style={{ gap: 'var(--sp-3)', paddingLeft: `${Math.min(r.depth, 6) * 12}px` }}>
                  <span class="mono grow ellipsis" title={r.path}>{r.path === '.' ? '(project)' : r.path}</span>
                  <span class="meta mono">{r.err ? r.err : `${r.branch || (r.head ? `detached ${r.head}` : '')}${r.dirty ? ` · ${r.dirty} changed` : ''}`}</span>
                  <select class="select" aria-label={`Changes in ${r.path}`} value={modes[r.path] || 'uncommitted'}
                    onChange={(e) => setModes({ ...modes, [r.path]: (e.target as HTMLSelectElement).value as RepoMode })}>
                    <option value="branch">On a work branch</option><option value="uncommitted">Left uncommitted</option>
                  </select>
                </div>
              ))}
              {repos?.more ? <span class="meta">{repos.more} more not shown</span> : null}
              {repos?.warn ? <span class="meta">{repos.warn}</span> : null}
            </div>
          </Field>
        ) : null}
        <Field label="Stages" hint={PRESETS.find((p) => p.value === preset)?.hint}>
          <select class="select" value={preset} onChange={(e) => setPreset((e.target as HTMLSelectElement).value as Preset)}>
            {PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </Field>
        {preset === 'custom' ? (
          <div class="row" role="group" aria-label="Stages" style={{ flexWrap: 'wrap', gap: 'var(--sp-3)' }}>
            {STAGES.map((s) => <label key={s} class="check"><input type="checkbox" checked={stages.includes(s)} onChange={() => toggleStage(s)} />{s}</label>)}
          </div>
        ) : null}
        <Field label="When the plan is written">
          <Seg label="When the plan is written" value={after} onChange={setAfter} options={[{ value: 'wait', label: 'Wait for my approval' }, { value: 'go', label: 'Go straight on' }, { value: 'defaults', label: 'Take the defaults' }]} />
        </Field>
        <Field label="Note for the agent" hint="Optional. Added to the ticket's notes.">
          <textarea class="textarea" rows={2} value={note} onInput={(e) => setNote((e.target as HTMLTextAreaElement).value)} />
        </Field>
        {err ? <div class="notice notice--warn" role="alert" data-testid="add-error"><Icon name="alert" />{err}</div> : null}
      </div>
      <footer>
        <span class="meta">Ctrl+Enter to add</span><span class="spacer" />
        <Button variant="ghost" onClick={closeDialog}>Cancel</Button>
        <Button variant="primary" icon="plus" testid="add-submit" disabled={!!off} title={off || undefined} onClick={submit}>Add to queue</Button>
      </footer>
    </div>
  );
}

/** `keys`: Jira keys to start with (the Jira view's picks). */
export function openAddTickets(keys = ''): void { openDialog(<AddTickets initialKeys={keys} />); }
