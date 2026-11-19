// One multiplexed conditional poll (D7, 4.5). Pure: transport, clock and sinks are injected (unit-tested with a fake clock).
export interface PollTopicResult { sig?: string; data?: unknown; same?: true; error?: { code: string; message: string } }
export type PollAnswer = { ok: true; stale: boolean; topics: Record<string, PollTopicResult> } | { ok: false; status: number };
export interface Clock { now(): number; set(fn: () => void, ms: number): unknown; clear(h: unknown): void }
export const realClock: Clock = { now: () => Date.now(), set: (fn, ms) => setTimeout(fn, ms), clear: (h) => clearTimeout(h as NodeJS.Timeout) };

export interface PollerDeps {
  fetch(q: { topic: string; sig: string }[]): Promise<PollAnswer>;
  update(topic: string, sig: string, data: unknown): void;
  health(ok: boolean, failures: number, nextInMs: number): void;
  stale?(stale: boolean): void;
  clock?: Clock;
}
export type Visibility = 'focused' | 'blurred' | 'hidden';

export const BACKOFF = [1000, 2000, 5000, 15000];
export const HIDDEN_TOPICS = new Set(['status', 'queue']);
export const MAX_TOPICS = 24;

export class Poller {
  private sets = new Map<string, Set<string>>();   // owner -> topics
  private sigs = new Map<string, string>();
  private last = new Map<string, unknown>();
  private timer: unknown = null;
  private inflight = false;
  private running = false;
  failures = 0;
  vis: Visibility = 'focused';
  private clock: Clock;
  constructor(private d: PollerDeps) { this.clock = d.clock || realClock; }

  topics(): string[] {
    const u = new Set<string>();
    for (const s of this.sets.values()) for (const t of s) u.add(t);
    let list = [...u];
    if (this.vis === 'hidden') list = list.filter((t) => HIDDEN_TOPICS.has(t));
    return list.slice(0, MAX_TOPICS);
  }

  /** Replace one owner's topic set. New topics are fetched right away; known ones re-emit their last data. */
  set(owner: string, topics: string[]): void {
    const prev = this.topics();
    this.sets.set(owner, new Set(topics.filter((t) => /^[a-z-]+(:[A-Za-z0-9._-]+)?$/.test(t))));
    const now = this.topics();
    for (const t of [...this.sigs.keys()]) if (!now.includes(t)) { this.sigs.delete(t); this.last.delete(t); }
    for (const t of now) if (this.last.has(t)) this.d.update(t, this.sigs.get(t) || '', this.last.get(t));
    if (this.running && now.some((t) => !prev.includes(t))) this.kick(0);
  }
  drop(owner: string): void { this.sets.delete(owner); }

  setVisibility(v: Visibility): void {
    if (v === this.vis) return;
    const wasHidden = this.vis === 'hidden';
    this.vis = v;
    if (this.running && (wasHidden || v === 'focused')) this.kick(0);
  }

  /** Whether something is running: runner active or a subscribed session working. */
  busy(): boolean {
    const q = this.last.get('queue') as { runner?: { running?: boolean } } | undefined;
    if (q?.runner?.running) return true;
    for (const [t, d] of this.last) if (t.startsWith('session:') && (d as { row?: { working?: boolean } })?.row?.working) return true;
    return false;
  }

  interval(): number {
    if (this.failures > 0) return BACKOFF[Math.min(this.failures - 1, BACKOFF.length - 1)];
    if (this.vis === 'hidden') return 15000;
    if (this.vis === 'blurred') return 5000;
    return this.busy() ? 1000 : 3000;
  }

  start(): void { if (this.running) return; this.running = true; this.kick(0); }
  stop(): void { this.running = false; if (this.timer !== null) this.clock.clear(this.timer); this.timer = null; }
  /** Forget sigs (e.g. after reconnecting to a different serve) so everything is re-sent. */
  reset(): void { this.sigs.clear(); this.last.clear(); this.failures = 0; }
  /** Poll as soon as possible (after an action, or on demand). */
  kick(ms = 0): void {
    if (!this.running) return;
    if (this.timer !== null) this.clock.clear(this.timer);
    this.timer = this.clock.set(() => { this.timer = null; void this.tick(); }, ms);
  }

  async tick(): Promise<void> {
    if (this.inflight || !this.running) return;
    const list = this.topics();
    if (!list.length) { this.schedule(); return; }
    this.inflight = true;
    let ans: PollAnswer;
    try { ans = await this.d.fetch(list.map((topic) => ({ topic, sig: this.sigs.get(topic) || '' }))); }
    catch { ans = { ok: false, status: 0 }; }
    this.inflight = false;
    if (!ans.ok) {
      this.failures++;
      this.d.health(false, this.failures, this.interval());
      this.schedule();
      return;
    }
    const wasFailing = this.failures > 0;
    this.failures = 0;
    this.d.stale?.(ans.stale);
    for (const [topic, r] of Object.entries(ans.topics)) {
      if (!list.includes(topic)) continue;
      if (r.error) continue;
      if (r.same) continue;
      if (typeof r.sig === 'string' && r.sig === this.sigs.get(topic)) continue;
      this.sigs.set(topic, r.sig || '');
      this.last.set(topic, r.data);
      this.d.update(topic, r.sig || '', r.data);
    }
    if (wasFailing) this.d.health(true, 0, this.interval());
    this.schedule();
  }

  private schedule(): void { if (this.running) this.kick(this.interval()); }
}

export function pollPath(q: { topic: string; sig: string }[]): string {
  return '/api/v1/poll?' + q.map((x) => `t=${encodeURIComponent(x.topic)}~${encodeURIComponent(x.sig)}`).join('&');
}
