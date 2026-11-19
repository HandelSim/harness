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
  /** A topic-level error in a poll answer. Without it the error goes to update(topic, '', { error }). */
  error?(topic: string, error: { code: string; message: string }): void;
  clock?: Clock;
}
export type Visibility = 'focused' | 'blurred' | 'hidden';

export const BACKOFF = [1000, 2000, 5000, 15000];
export const HIDDEN_TOPICS = new Set(['status', 'queue']);
/** Server cap per /poll request; more topics are split over several requests. */
export const MAX_TOPICS = 24;

export class Poller {
  private sets = new Map<string, Set<string>>();   // owner -> topics
  private sigs = new Map<string, string>();
  private last = new Map<string, unknown>();
  private errs = new Map<string, string>();         // topic -> last forwarded error (code + message)
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
    const list = [...u];
    return this.vis === 'hidden' ? list.filter((t) => HIDDEN_TOPICS.has(t)) : list;
  }

  /**
   * Replace one owner's topic set. Topics new to the poller are fetched right away; topics new to this owner but
   * already cached re-emit their last data once. Topics the owner already had are not re-sent (M1).
   */
  set(owner: string, topics: string[]): void {
    const prev = new Set(this.topics());
    const had = this.sets.get(owner) || new Set<string>();
    const mine = new Set(topics.filter((t) => /^[a-z-]+(:[A-Za-z0-9._-]+)?$/.test(t)));
    this.sets.set(owner, mine);
    const now = this.topics();
    for (const t of [...this.sigs.keys()]) if (!now.includes(t)) { this.sigs.delete(t); this.last.delete(t); }
    for (const t of [...this.errs.keys()]) if (!now.includes(t)) this.errs.delete(t);
    for (const t of now) if (mine.has(t) && !had.has(t) && this.last.has(t)) this.d.update(t, this.sigs.get(t) || '', this.last.get(t));
    if (this.running && now.some((t) => !prev.has(t))) this.kick(0);
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
    // A running queue or working session keeps the fast cadence while the window is visible but unfocused (L13).
    if (this.busy()) return this.vis === 'blurred' ? 2000 : 1000;
    return this.vis === 'blurred' ? 5000 : 3000;
  }

  start(): void { if (this.running) return; this.running = true; this.kick(0); }
  stop(): void { this.running = false; if (this.timer !== null) this.clock.clear(this.timer); this.timer = null; }
  /** Forget sigs (e.g. after reconnecting to a different serve) so everything is re-sent. */
  reset(): void { this.sigs.clear(); this.last.clear(); this.errs.clear(); this.failures = 0; }
  /** Poll now, skipping the current wait or backoff (e.g. on powerMonitor resume / unlock-screen). */
  pollNow(): void { this.kick(0); }
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
    const q = list.map((topic) => ({ topic, sig: this.sigs.get(topic) || '' }));
    const chunks: (typeof q)[] = [];
    for (let i = 0; i < q.length; i += MAX_TOPICS) chunks.push(q.slice(i, i + MAX_TOPICS));
    let ans: PollAnswer;
    try { ans = merge(await Promise.all(chunks.map((c) => this.d.fetch(c)))); }
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
      if (r.error) {
        // Forward it once per distinct error (M6) so the renderer marks the topic stale; keep sig and last data.
        const k = `${r.error.code}\n${r.error.message}`;
        if (this.errs.get(topic) === k) continue;
        this.errs.set(topic, k);
        if (this.d.error) this.d.error(topic, r.error); else this.d.update(topic, '', { error: r.error });
        continue;
      }
      const recovered = this.errs.delete(topic);
      const unchanged = !!r.same || (typeof r.sig === 'string' && r.sig === this.sigs.get(topic));
      if (unchanged) {
        // Recovered with the same data: re-send it so the renderer clears the error (same sig, so no re-render).
        if (recovered && this.last.has(topic)) this.d.update(topic, this.sigs.get(topic) || '', this.last.get(topic));
        continue;
      }
      this.sigs.set(topic, r.sig || '');
      this.last.set(topic, r.data);
      this.d.update(topic, r.sig || '', r.data);
    }
    if (wasFailing) this.d.health(true, 0, this.interval());
    this.schedule();
  }

  private schedule(): void { if (this.running) this.kick(this.interval()); }
}

function merge(answers: PollAnswer[]): PollAnswer {
  const topics: Record<string, PollTopicResult> = {};
  let stale = false;
  for (const a of answers) {
    if (!a.ok) return a;
    stale = stale || a.stale;
    Object.assign(topics, a.topics);
  }
  return { ok: true, stale, topics };
}

export function pollPath(q: { topic: string; sig: string }[]): string {
  return '/api/v1/poll?' + q.map((x) => `t=${encodeURIComponent(x.topic)}~${encodeURIComponent(x.sig)}`).join('&');
}
