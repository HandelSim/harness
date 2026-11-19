// Windowed list for fixed-height rows (M1 list half, PLAN 3.4 "virtualised above N rows"). Only the rows in view plus
// an overscan are in the DOM; spacers keep the scroll height. Depends on preact only (rendered in a unit test).
import type { ComponentChildren, HTMLAttributes } from 'preact';
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';

/** Lists at or below this size render every row (no spacers). */
export const VIRTUAL_MIN = 200;
const OVERSCAN = 8;
const DEFAULT_VIEW = 800;      // before the first measurement

/** Row range [start, end) to render for a scroll position. */
export function windowRange(scrollTop: number, viewH: number, rowH: number, count: number, overscan = OVERSCAN): { start: number; end: number } {
  const v = viewH > 0 ? viewH : DEFAULT_VIEW;
  const start = Math.max(0, Math.min(count, Math.floor(Math.max(0, scrollTop) / rowH) - overscan));
  const end = Math.max(start, Math.min(count, Math.ceil((Math.max(0, scrollTop) + v) / rowH) + overscan));
  return { start, end };
}

/** New scrollTop that brings row i fully into view, or null when it already is. */
export function scrollToRow(i: number, rowH: number, scrollTop: number, viewH: number): number | null {
  const top = i * rowH;
  if (top < scrollTop) return top;
  if (viewH > 0 && top + rowH > scrollTop + viewH) return top + rowH - viewH;
  return null;
}

type DivProps = Omit<HTMLAttributes<HTMLDivElement>, 'children' | 'ref' | 'onScroll'>;
export function VirtualList<T>({ items, rowHeight, row, focusIndex, min = VIRTUAL_MIN, ...div }: DivProps & {
  items: T[]; rowHeight: number; row: (item: T, index: number) => ComponentChildren;
  /** Index to keep in view (keyboard selection); scrolled to when it changes. */
  focusIndex?: number; min?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ top: 0, h: 0 });
  const measure = () => { const el = ref.current; if (el) setView((v) => (v.top === el.scrollTop && v.h === el.clientHeight ? v : { top: el.scrollTop, h: el.clientHeight })); };
  useLayoutEffect(() => {
    measure();
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    const el = ref.current;
    if (!el || focusIndex === undefined || focusIndex < 0) return;
    const t = scrollToRow(focusIndex, rowHeight, el.scrollTop, el.clientHeight);
    if (t !== null) { el.scrollTop = t; measure(); }
  }, [focusIndex]);
  const n = items.length;
  const virtual = n > min;
  const { start, end } = virtual ? windowRange(view.top, view.h, rowHeight, n) : { start: 0, end: n };
  const out: ComponentChildren[] = [];
  for (let i = start; i < end; i++) out.push(row(items[i], i));
  return (
    <div {...div} ref={ref} onScroll={virtual ? measure : undefined} data-virtual={virtual ? '1' : undefined}>
      {virtual && start > 0 ? <div class="vlist-pad" style={{ height: `${start * rowHeight}px` }} aria-hidden="true" /> : null}
      {out}
      {virtual && end < n ? <div class="vlist-pad" style={{ height: `${(n - end) * rowHeight}px` }} aria-hidden="true" /> : null}
    </div>
  );
}
