// Context-loss watchdog (review2 P1-5). After a WebGL context loss the worker waits for `webglcontextrestored`;
// a GPU process that is blocklisted after a crash, or a driver reset without restore, never sends it and the layer
// stays blank. arm() after the loss; a newer frame of that layer clears it; if none arrives within `ms` the engine
// gives up on GL and falls to the next rung. While the scene cannot draw anyway (paused / hidden) it waits.
export interface RestoreWatchDeps {
  setTimeout: (f: () => void, ms: number) => unknown;
  clearTimeout: (h: unknown) => void;
  /** false while no frame is expected even with a live context (paused / hidden): check again later. */
  canJudge: () => boolean;
  onGiveUp: (id: number) => void;
  ms?: number;
}
export const RESTORE_WAIT_MS = 4000;

export class RestoreWatch {
  private id = -1; private frames = 0; private h: unknown = null;
  constructor(private d: RestoreWatchDeps) {}
  /** Layer id being watched, or -1. */
  get watching(): number { return this.id; }
  arm(id: number, frames: number): void { this.clear(); this.id = id; this.frames = frames; this.schedule(); }
  /** A stats report from the worker: a frame count past the one at loss means the context came back. */
  frame(id: number, frames: number): void { if (id === this.id && frames > this.frames) this.clear(); }
  clear(): void { if (this.h !== null) this.d.clearTimeout(this.h); this.h = null; this.id = -1; }
  private schedule(): void {
    this.h = this.d.setTimeout(() => {
      this.h = null;
      if (this.id < 0) return;
      if (!this.d.canJudge()) { this.schedule(); return; }
      const id = this.id; this.id = -1;
      this.d.onGiveUp(id);
    }, this.d.ms ?? RESTORE_WAIT_MS);
  }
}
