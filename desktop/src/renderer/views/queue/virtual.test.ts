import { test } from 'node:test';
import assert from 'node:assert/strict';
import { h, render } from 'preact';
import { VirtualList, windowRange, scrollToRow, VIRTUAL_MIN } from './virtual';

// The smallest DOM preact needs to render divs and text (no DOM library in this repo).
class FNode {
  childNodes: FNode[] = [];
  parentNode: FNode | null = null;
  get firstChild() { return this.childNodes[0] ?? null; }
  get nextSibling(): FNode | null { const p = this.parentNode; if (!p) return null; return p.childNodes[p.childNodes.indexOf(this) + 1] ?? null; }
  appendChild(c: FNode) { return this.insertBefore(c, null); }
  insertBefore(c: FNode, ref: FNode | null) {
    if (c.parentNode) c.parentNode.removeChild(c);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(c); else this.childNodes.splice(i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c: FNode) { const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); c.parentNode = null; return c; }
  remove() { this.parentNode?.removeChild(this); }
}
class FText extends FNode { nodeType = 3; constructor(public data: string) { super(); } }
class FEl extends FNode {
  nodeType = 1; attrs = new Map<string, string>(); scrollTop = 0; clientHeight = 320;
  style: Record<string, unknown> = { setProperty(k: string, v: string) { (this as Record<string, unknown>)[k] = v; } };
  constructor(public localName: string) { super(); }
  get nodeName() { return this.localName.toUpperCase(); }
  setAttribute(k: string, v: unknown) { this.attrs.set(k, String(v)); }
  getAttribute(k: string) { return this.attrs.get(k) ?? null; }
  removeAttribute(k: string) { this.attrs.delete(k); }
  addEventListener() {} removeEventListener() {}
  all(): FEl[] { return this.childNodes.flatMap((c) => (c instanceof FEl ? [c, ...c.all()] : [])); }
}
(globalThis as Record<string, unknown>).document = {
  createElement: (t: string) => new FEl(t), createElementNS: (_n: string, t: string) => new FEl(t), createTextNode: (s: string) => new FText(s),
};

const tick = () => new Promise((r) => setTimeout(r, 60));
const rows = (root: FEl) => root.all().filter((e) => e.attrs.get('data-testid')?.startsWith('row-'));

test('windowRange / scrollToRow', () => {
  assert.deepEqual(windowRange(0, 320, 32, 2000, 8), { start: 0, end: 18 });
  assert.deepEqual(windowRange(32 * 1000, 320, 32, 2000, 8), { start: 992, end: 1018 });
  assert.deepEqual(windowRange(32 * 5000, 320, 32, 2000, 8), { start: 2000, end: 2000 });
  assert.equal(scrollToRow(5, 32, 0, 320), null);
  assert.equal(scrollToRow(20, 32, 0, 320), 20 * 32 + 32 - 320);
  assert.equal(scrollToRow(2, 32, 320, 320), 64);
});

test('VirtualList renders only a window of 2,000 rows, keeps the scroll height, follows the focused row', async () => {
  const root = new FEl('body');
  const items = Array.from({ length: 2000 }, (_, i) => `K-${i}`);
  const row = (k: string) => h('div', { key: k, 'data-testid': `row-${k}`, class: 'qrow' }, k);
  const t0 = performance.now();
  render(h(VirtualList<string>, { items, rowHeight: 32, row, class: 'qlist', role: 'listbox' }), root as never);
  await tick();
  const ms = performance.now() - t0;
  const list = root.childNodes[0] as FEl;
  assert.equal(list.attrs.get('data-virtual'), '1');
  const n = rows(root).length;
  assert.ok(n > 0 && n < 60, `only a window is in the DOM (got ${n} of 2000)`);
  const pads = list.childNodes.filter((c) => c instanceof FEl && c.attrs.get('class') === 'vlist-pad') as FEl[];
  const padH = pads.reduce((s, p) => s + parseInt(String(p.style.height), 10), 0);
  assert.equal(padH + n * 32, 2000 * 32, 'spacers + rows = full scroll height');
  // keyboard selection far down: the list scrolls and that row is rendered
  render(h(VirtualList<string>, { items, rowHeight: 32, row, class: 'qlist', focusIndex: 1500 }), root as never);
  await tick();
  assert.ok(list.scrollTop > 0);
  assert.ok(rows(root).some((e) => e.attrs.get('data-testid') === 'row-K-1500'));
  assert.ok(rows(root).length < 60);
  assert.ok(ms < 1000, `first render of 2k items took ${ms.toFixed(0)} ms`);
  render(null, root as never);
});

test('VirtualList renders every row of a small list (no spacers)', async () => {
  const root = new FEl('body');
  const items = Array.from({ length: VIRTUAL_MIN }, (_, i) => `K-${i}`);
  render(h(VirtualList<string>, { items, rowHeight: 32, row: (k: string) => h('div', { key: k, 'data-testid': `row-${k}` }, k) }), root as never);
  await tick();
  assert.equal(rows(root).length, VIRTUAL_MIN);
  assert.equal((root.childNodes[0] as FEl).attrs.get('data-virtual'), undefined);
  render(null, root as never);
});
