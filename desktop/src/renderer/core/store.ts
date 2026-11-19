// Topic store (PLAN 3.2, 4.5): ref-counted subscriptions to live topics. Pure: IO is injected, so it is
// unit-tested in node. Main's poller pushes 'live:update' only on change; the store keeps the last data
// forever (stale marking instead of clearing), so a view switch renders cached data immediately.
import { batch, signal, type Signal } from '@preact/signals-core';
import type { ApiError, ReadResult, Topic, TopicData } from '../../shared/api';

export interface TopicEntry<T = unknown> {
  topic: Topic;
  data: Signal<T | undefined>;
  sig: string;
  error: Signal<ApiError['error'] | null>;
  /** 404/501 from the endpoint: the view shows "Open web version" (PLAN 4.6). */
  unavailable: Signal<boolean>;
  gone: Signal<boolean>;
  stale: Signal<boolean>;
  at: Signal<number>;          // ms of the last data change (0 = never loaded)
  refs: number;
}

export interface StoreIO {
  /** Tell main which topics to poll (union of all with refs > 0). */
  setTopics(topics: Topic[]): void;
  /** First load of an uncached topic (GET of its endpoint). */
  fetch(topic: Topic, sig: string): Promise<ReadResult<unknown>>;
  now(): number;
}

/** Topic -> GET path below /api/v1 (4.3). */
export function topicPath(t: Topic): string {
  const i = t.indexOf(':');
  const base = i < 0 ? t : t.slice(0, i);
  const arg = i < 0 ? '' : encodeURIComponent(t.slice(i + 1));
  switch (base) {
    case 'ticket': return `/tickets/${arg}`;
    case 'session': return `/sessions/${arg}`;
    case 'findings-agent': return '/findings/agent';
    case 'ingest': return '/vault/ingest';
    default: return `/${base}`;
  }
}

export class TopicStore {
  private entries = new Map<Topic, TopicEntry>();
  private pushQueued = false;
  private lastPushed = '';
  constructor(private io: StoreIO, private schedule: (fn: () => void) => void = (fn) => queueMicrotask(fn)) {}

  entry<T extends Topic>(t: T): TopicEntry<TopicData<T>> {
    let e = this.entries.get(t);
    if (!e) {
      e = { topic: t, data: signal(undefined), sig: '', error: signal(null), unavailable: signal(false), gone: signal(false),
        stale: signal(false), at: signal(0), refs: 0 };
      this.entries.set(t, e);
    }
    return e as TopicEntry<TopicData<T>>;
  }

  /** Subscribe; returns the unsubscribe function. First subscriber of an unloaded topic triggers a fetch. */
  subscribe<T extends Topic>(t: T): () => void {
    const e = this.entry(t);
    e.refs++;
    if (e.refs === 1) {
      this.queuePush();
      if (!e.at.value && !e.unavailable.value) void this.load(t);
    }
    let done = false;
    return () => {
      if (done) return;
      done = true;
      e.refs = Math.max(0, e.refs - 1);
      if (e.refs === 0) this.queuePush();
    };
  }

  /** Force a re-read (after an action, or Retry on an error panel). */
  async load(t: Topic): Promise<void> {
    const e = this.entry(t);
    let r: ReadResult<unknown>;
    try { r = await this.io.fetch(t, e.sig); } catch (err) {
      r = { ok: false, status: 0, error: { code: 'offline', message: String((err as Error)?.message || err) } };
    }
    if (r.ok) {
      if (r.same) { e.stale.value = false; e.error.value = null; return; }
      this.apply(t, r.sig, r.data);
    } else {
      if (r.status === 404 && (t.startsWith('ticket:') || t.startsWith('session:')) && r.error.code === 'not-found') {
        this.apply(t, '', { gone: true });
        return;
      }
      if (r.status === 404 || r.status === 501) e.unavailable.value = true;
      e.error.value = r.error;
      if (e.at.value) e.stale.value = true;
    }
  }

  /** A 'live:update' push, or a fetch result. */
  apply(t: Topic, sig: string, data: unknown): void {
    const e = this.entry(t);
    if (sig && sig === e.sig && e.at.value) {
      // Same content (a re-send, or a recovery after a topic error): clear the marks, keep the data identity so
      // subscribers do not re-render (M1).
      batch(() => { e.error.value = null; e.unavailable.value = false; e.stale.value = false; });
      return;
    }
    e.sig = sig;
    batch(() => {                     // one render per change, not one per field
      e.error.value = null;
      e.unavailable.value = false;
      e.stale.value = false;
      const gone = !!data && typeof data === 'object' && (data as { gone?: unknown }).gone === true;
      e.gone.value = gone;
      if (!gone) e.data.value = data as never;
      e.at.value = this.io.now();
    });
  }

  /** A topic-level error inside a /poll answer. */
  fail(t: Topic, error: ApiError['error'], status = 0): void {
    const e = this.entry(t);
    const cur = e.error.value;
    if (!cur || cur.code !== error.code || cur.message !== error.message) e.error.value = error;
    if (status === 404 || status === 501 || error.code === 'unavailable' || error.code === 'not-found') e.unavailable.value = true;
    if (e.at.value) e.stale.value = true;
  }

  /** Backend offline / poll failing: keep data, mark it stale. */
  markAllStale(stale: boolean): void {
    for (const e of this.entries.values()) if (e.at.value) e.stale.value = stale;
  }

  /** Topics with at least one subscriber, sorted (what main polls). */
  active(): Topic[] {
    return [...this.entries.values()].filter((e) => e.refs > 0).map((e) => e.topic).sort();
  }

  /** After a reconnect: re-send the topic list even if unchanged. */
  resubscribe(): void { this.lastPushed = ''; this.queuePush(); }

  private queuePush(): void {
    if (this.pushQueued) return;
    this.pushQueued = true;
    this.schedule(() => {
      this.pushQueued = false;
      const list = this.active();
      const key = list.join('\n');
      if (key === this.lastPushed) return;
      this.lastPushed = key;
      this.io.setTopics(list);
    });
  }
}
