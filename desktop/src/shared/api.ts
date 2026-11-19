// FILE: desktop/src/shared/api.ts
// Wire contract of serve/api.py. Fields listed are the ones the UI relies on;
// objects marked "pass-through" carry every key the Python side has and the UI
// must ignore keys it does not know.
export const API_VERSION = 1;
export const API_PREFIX = '/api/v1';

export type ApiErrorCode =
  | 'bad-request' | 'auth' | 'origin' | 'not-found' | 'method' | 'refused' | 'changed'
  | 'too-large' | 'content-type' | 'internal' | 'unavailable'
  | 'offline' | 'timeout' | 'unknown-result';            // last three are made by main, never by serve
export interface ApiError { ok: false; status: number; error: { code: ApiErrorCode; message: string } }
export interface ReadOk<T> { ok: true; sig: string; data?: T; same?: true }
export interface ActionOk { ok: true; msg: string; data?: Record<string, unknown> }
export type ReadResult<T> = ReadOk<T> | ApiError;
export type ActionResult = ActionOk | ApiError;
export interface Gate { ok: boolean; reason: string }    // reason: why a control is disabled ('' when ok)

export interface Status {
  api: number; mode: 'serve' | 'viewer'; pid: number; state_id: string; stale: boolean;
  root: string; state_dir: string; codes: boolean; user: string | null; runs: boolean;
  opencode: { port: number; url: string } | null;
  proxy: { url: string; up: boolean; detail: string } | null;
  vault: { name: string; dir: string } | null;
  modes: { name: string; description: string }[];
  version: { branch: string; sha: string; describe: string };
}

export interface SessionRow {
  id: string; parent: string | null; source: string;     // 'container' | 'host'
  directory: string; title: string; agent: string; model: string;
  created: number; updated: number;                      // epoch ms
  tokens_in: number; tokens_out: number; cost: number; kids: number;
  working: boolean;                                      // updated within 90 s or live
  live: boolean;                                         // relay alive: can be messaged
  ticket: string | null;                                 // queue ticket key
  kind: 'session' | 'queue' | 'debug' | 'findings'; label: string;
  web: string | null;                                    // opencode-web URL for this session, when there is one
}
export interface InboxItem { at: number; sender: string; state: string; text: string }
export interface SessionHead {
  row: SessionRow; count: number;                        // number of messages
  children: { id: string; title: string; agent: string; created: number; updated: number }[];
  inbox: InboxItem[]; send: Gate; stop: Gate;
}
export interface Part { id: string; type: string; truncated?: boolean; [k: string]: unknown }  // opencode part, pass-through
export interface Message { id: string; index: number; created: number; role: string; info: Record<string, unknown>; parts: Part[] }
export interface MessagesPage { from: number; count: number; messages: Message[]; working: boolean; live: boolean }

export type TicketStatus = 'queued' | 'running' | 'interrupted' | 'needs-answers' | 'plan-review' | 'blocked' | 'stopped' | 'done';
export type TicketAction = 'answer' | 'approve' | 'retry' | 'continue' | 'stop' | 'remove' | 'move' | 'note'
  | 'followup' | 'edit' | 'fix' | 'archive' | 'unarchive';
export interface Runner {                                 // Runs.runner(), pass-through
  running: boolean; starting: boolean; halting: boolean; exited: unknown;
  ticket: string; stage: string; step: string; model: string; started: unknown; since: unknown;
  host: boolean; single: boolean; watch: boolean; parallel: number; on: unknown[]; pid: number; beat: unknown;
  [k: string]: unknown;
}
export interface TicketSummary {                          // subset of ticket_state for list rows
  key: string; title: string; status: TicketStatus; stage: string; step: string; reason: string;
  created: unknown; updated: unknown; finished: unknown; attempt: number; project: string; branch: string;
  stopping: boolean; debug: boolean; session: { stage: string; id: string } | null;
  actions: Record<TicketAction, Gate>;
}
export interface Queue {
  runner: Runner; tickets: TicketSummary[]; archived: TicketSummary[];
  start: Gate;                                            // may a run be started now
}
export interface TicketState {                            // Runs.ticket_state(), pass-through (INV 3 lists every field)
  status: TicketStatus; stage: string; title: string; reason: string; plan_sig: string;
  criteria: { id: string; text: string; [k: string]: unknown }[];
  questions: { id: string; text: string; ask: string; blocking: boolean; default: string; options: unknown[] }[];
  notes: { at: unknown; text: string }[];
  history: { at: unknown; stage: string; attempt: number; result: string; details: string }[];
  [k: string]: unknown;
}
export interface Ticket {
  key: string; archived: boolean; state: TicketState;
  actions: Record<TicketAction, Gate>;
  retry_choices: { value: string; label: string }[];
  sessions: { sid: string; stage: string; label: string; kind: string; working: boolean; created: number }[];
  outs: { name: string; mtime: number; size: number }[];
  log: string;                                            // log.md text (capped 256 KiB, newest kept)
}
export interface OutChunk { name: string; from: number; next: number; size: number; text: string; reset: boolean }
export interface Diff {
  repos: { rel: string; label: string }[]; views: string[]; repo: string; view: string;
  diff: string; commits: { sha: string; subject: string }[]; notes: unknown; says: unknown; rev: string; error: string;
}
export interface JiraRow { key: string; summary: string; status: string; assignee: string; type: string; priority: string;
  queued: TicketStatus | null; [k: string]: unknown }
export interface Jira { rows: JiraRow[]; error: string; note: string; more: boolean; projects: string[] }
export interface Finding { id: string; title: string; archived: boolean; [k: string]: unknown }      // pass-through
export interface Findings { items: Finding[]; order: string[] }
export interface FindingsAgent { running: boolean; status: Record<string, unknown>; passes: Record<string, unknown>[];
  folders: unknown; last_repos: unknown; notes: string; knowledge: { slug: string; name: string; title: string }[] }
export interface Vaults { active: string; vaults: { name: string; url: string }[] }
export interface Ingest { run: Record<string, unknown> | null; summary: unknown; out_tail: string }
export interface VaultOverview { name: string; dir: string; facts: unknown; pages: { rel: string; title: string }[];
  ingest: Ingest; log: unknown }
export interface Home { live: SessionRow[]; usage: Record<string, unknown> | null;   // read_ledger(): {day,hour,models,remaining}
  pages: { rel: string; title: string }[]; activity: unknown }
export interface ConfigRow { key: string; value: string; secret: boolean; desc: string; group: string }
export interface SettingsData { config: ConfigRow[]; allowlist: { host: string; push: boolean; note: string }[];
  mcp: { name: string; state: string; status: string }[]; access: { enabled: boolean; names: string[] };
  system: Record<string, unknown> }
export interface HarnessUpdate { branch: string; local: string; remote: string | null; behind: boolean | null; checked_at: number | null }
export interface DebugReport { kind: string; name: string; mtime: number; key: string; verdict: string; change: boolean;
  title: string; session: string; report?: boolean }

export interface TopicMap {
  status: Status; home: Home; queue: Queue; sessions: SessionRow[]; findings: Findings;
  'findings-agent': FindingsAgent; vault: VaultOverview; ingest: Ingest; debug: DebugReport[];
}
export type Topic = keyof TopicMap | `ticket:${string}` | `session:${string}`;
export type TopicData<T extends Topic> =
  T extends keyof TopicMap ? TopicMap[T] :
  T extends `ticket:${string}` ? Ticket | { gone: true } :
  T extends `session:${string}` ? SessionHead | { gone: true } : never;
