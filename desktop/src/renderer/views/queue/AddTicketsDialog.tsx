// Add tickets dialog (W5): Jira keys or a ticket of your own, project, starting point, stages, after-plan, note.
// The server validates everything; a refusal is shown inline and every field is kept.
import { useState } from 'preact/hooks';
import { act, appState, patchState, offlineReason } from '../../core/app';
import { bridge } from '../../core/bridge';
import { openDialog, closeDialog } from '../../shell/ui';
import { Button, Field, Seg } from '../../ui/kit';
import { Icon } from '../../ui/Icon';

type Kind = 'jira' | 'local';
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

function AddTickets() {
  const recents = appState.value?.recents?.projects ?? [];
  const [kind, setKind] = useState<Kind>('jira');
  const [keys, setKeys] = useState('');
  const [title, setTitle] = useState('');
  const [desc, setDesc] = useState('');
  const [project, setProject] = useState(recents[0] ?? '');
  const [start, setStart] = useState<Start>('');
  const [cont, setCont] = useState('');
  const [preset, setPreset] = useState<Preset>('full');
  const [stages, setStages] = useState<string[]>(['plan', 'implement', 'review', 'fix', 'push']);
  const [after, setAfter] = useState<'wait' | 'go' | 'defaults'>('wait');
  const [note, setNote] = useState('');
  const [err, setErr] = useState('');
  const off = offlineReason.value;

  const submit = async () => {
    setErr('');
    const body: Record<string, unknown> = { kind, project, preset, after_plan: after };
    if (kind === 'jira') body.keys = keys; else { body.title = title; body.desc = desc; }
    if (start) body.start = start;
    if (start === 'continue') body.cont = cont;
    if (preset === 'custom') body.stages = stages;
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
        </Field>
        <Field label="Starting point">
          <Seg label="Starting point" value={start} onChange={setStart} options={[{ value: '', label: 'Default' }, { value: 'new', label: 'New branch' }, { value: 'continue', label: 'Continue existing work' }, { value: 'here', label: 'Here, as it is' }]} />
        </Field>
        {start === 'continue' ? <Field label="Continue from" hint="The branch (or commit) the work is on."><input class="input mono" value={cont} onInput={(e) => setCont((e.target as HTMLInputElement).value)} /></Field> : null}
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

export function openAddTickets(): void { openDialog(<AddTickets />); }
