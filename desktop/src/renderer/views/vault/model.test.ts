import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  bytesPath, filterPages, flattenTree, ingestFailures, ingestSources, ingestStatus, linkify, parseVaultHref, resolveRel,
  vaultHref, wikiTarget, type TreeEntry,
} from './model';

const pages = [{ rel: 'wiki/index.md', title: 'Index' }, { rel: 'wiki/code/api.md', title: 'API' }, { rel: 'wiki/notes/Plan.md', title: 'The plan' }];

test('filterPages: title or path, case-insensitive, sorted by path', () => {
  assert.deepEqual(filterPages(pages, '').map((p) => p.rel), ['wiki/code/api.md', 'wiki/index.md', 'wiki/notes/Plan.md']);
  assert.deepEqual(filterPages(pages, 'PLAN').map((p) => p.rel), ['wiki/notes/Plan.md']);
  assert.deepEqual(filterPages(pages, 'code/').map((p) => p.rel), ['wiki/code/api.md']);
  assert.deepEqual(filterPages(pages, 'zzz'), []);
});

test('flattenTree: open folders expand depth first, folders before files', () => {
  const e = (path: string, dir = false): TreeEntry => ({ name: path.split('/').pop()!, path, dir, size: 1 });
  const kids = { '': [e('a.md'), e('wiki', true), e('img', true)], wiki: [e('wiki/x.md'), e('wiki/sub', true)], 'wiki/sub': [e('wiki/sub/y.md')] };
  assert.deepEqual(flattenTree(kids, new Set()).map((r) => r.e.path), ['wiki', 'img', 'a.md']);
  const open = flattenTree(kids, new Set(['wiki', 'wiki/sub']), new Set(['img']));
  assert.deepEqual(open.map((r) => [r.e.path, r.depth]), [['wiki', 0], ['wiki/sub', 1], ['wiki/sub/y.md', 2], ['wiki/x.md', 1], ['img', 0], ['a.md', 0]]);
  assert.equal(open.find((r) => r.e.path === 'img')!.loading, true);
  assert.equal(open[0].open, true);
  // an open folder whose children are not loaded yet shows just itself
  assert.deepEqual(flattenTree({ '': [e('d', true)] }, new Set(['d'])).map((r) => r.e.path), ['d']);
});

test('resolveRel: relative, parent, absolute-in-vault, escapes, externals, anchors and queries', () => {
  assert.equal(resolveRel('wiki/notes/Plan.md', 'other.md'), 'wiki/notes/other.md');
  assert.equal(resolveRel('wiki/notes/Plan.md', '../code/api.md#x'), 'wiki/code/api.md');
  assert.equal(resolveRel('wiki/notes/Plan.md', './a%20b.md?y=1'), 'wiki/notes/a b.md');
  assert.equal(resolveRel('wiki/notes/Plan.md', '/wiki/index.md'), 'wiki/index.md');
  assert.equal(resolveRel('wiki/Plan.md', '../../etc/passwd'), null);
  assert.equal(resolveRel('a.md', '.git/config'), null);
  assert.equal(resolveRel('a.md', 'https://x.org/a.md'), null);
  assert.equal(resolveRel('a.md', 'mailto:me@x.org'), null);
  assert.equal(resolveRel('a.md', '#top'), null);
  assert.equal(resolveRel('a.md', '//host/x'), null);
});

test('wikiTarget: exact path, with or without .md, else basename; unknown is null', () => {
  assert.equal(wikiTarget('wiki/code/api', pages), 'wiki/code/api.md');
  assert.equal(wikiTarget('plan', pages), 'wiki/notes/Plan.md');
  assert.equal(wikiTarget('Index.md#Part', pages), 'wiki/index.md');
  assert.equal(wikiTarget('missing', pages), null);
});

test('vaultHref round-trips', () => {
  const h = vaultHref('wiki/a b#.md');
  assert.ok(h.startsWith('#vault:'));
  assert.equal(parseVaultHref(h), 'wiki/a b#.md');
  assert.equal(parseVaultHref('https://x'), null);
  assert.equal(parseVaultHref(null), null);
});

test('linkify: relative links, images and wiki links become in-app; code and externals are untouched', () => {
  const md = [
    'See [the API](../code/api.md) and [web](https://x.org).',
    '![diagram](img/d.png "t")',
    'Also [[Plan|our plan]] and [[nowhere]].',
    '`[x](y.md)` stays',
    '```',
    '[[Plan]] in code',
    '```',
  ].join('\n');
  const out = linkify(md, 'wiki/notes/Plan.md', pages);
  assert.match(out, /\[the API\]\(#vault:wiki%2Fcode%2Fapi\.md\)/);
  assert.match(out, /\[web\]\(https:\/\/x\.org\)/);
  assert.match(out, /\[image: diagram\]\(#vault:wiki%2Fnotes%2Fimg%2Fd\.png\)/);
  assert.match(out, /\[our plan\]\(#vault:wiki%2Fnotes%2FPlan\.md\)/);
  assert.match(out, / nowhere\./);
  assert.match(out, /`\[x\]\(y\.md\)` stays/);
  assert.match(out, /\[\[Plan\]\] in code/);
});

test('ingestStatus: running, stale, failed, ingested, never', () => {
  assert.equal(ingestStatus({ run: { alive: true }, summary: null }).running, true);
  assert.equal(ingestStatus({ run: { alive: false }, summary: null }).stale, true);
  assert.deepEqual(ingestStatus({ run: null, summary: { nfailed: 2, has: true } }).label, '2 units failed');
  assert.equal(ingestStatus({ run: null, summary: { has: true } }).label, 'Ingested');
  assert.equal(ingestStatus(null).label, 'Not ingested yet');
});

test('ingestSources / ingestFailures normalize junk', () => {
  assert.deepEqual(ingestSources(null), []);
  assert.deepEqual(ingestSources({ sources: [{ name: 'repo', ok: 3, pages: 2 }, 5] }),
    [{ name: 'repo', path: '', exists: true, ok: 3, failed: 0, pages: 2, at: '' }]);
  assert.deepEqual(ingestFailures({ failed: [{ id: 'code:x/a', problems: ['p', 2] }] }),
    [{ id: 'code:x/a', page: '', attempts: 0, at: '', problems: ['p', '2'] }]);
});

test('bytesPath encodes each segment', () => {
  assert.equal(bytesPath('wiki/a b/c#.png'), '/vault/file/wiki/a%20b/c%23.png');
});
