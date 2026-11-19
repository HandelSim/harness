import { test } from 'node:test';
import assert from 'node:assert/strict';
import { senderOk, apiPathOk, bytesPathOk, backendUrlOk, externalUrlOk, appUrlToFile, webviewSrcOk, CSP } from './security';

test('senderOk only for app://harness/', () => {
  assert.equal(senderOk('app://harness/index.html'), true);
  for (const u of ['app://harnessx/', 'http://127.0.0.1/', 'file:///index.html', '', null, undefined]) assert.equal(senderOk(u as string), false);
});
test('apiPathOk refuses traversal and foreign prefixes', () => {
  assert.equal(apiPathOk('/api/v1/queue'), true);
  assert.equal(apiPathOk('/api/v1/tickets/ABC-1/out/plan.md'), true);
  for (const p of ['/api/v2/x', '/login', '/api/v1/../x', '/api/v1/%2e%2e/x', '/api/v1/a//b', '/api/v1/a\\b', '/api/v1/a\0', 'api/v1/x', 42])
    assert.equal(apiPathOk(p), false, String(p));
});
test('bytesPathOk only /vault/file/', () => {
  assert.equal(bytesPathOk('/vault/file/img/a.png'), true);
  assert.equal(bytesPathOk('/vault/file/../../etc/passwd'), false);
  assert.equal(bytesPathOk('/api/v1/queue'), false);
});
test('backendUrlOk: loopback http with a port only', () => {
  assert.equal(backendUrlOk('http://127.0.0.1:8780'), true);
  assert.equal(backendUrlOk('http://localhost:8780'), true);
  for (const u of ['http://127.0.0.1', 'https://127.0.0.1:8780', 'http://10.0.0.1:8780', 'http://u:p@127.0.0.1:8780', 'http://127.0.0.1:8780/x', 'http://127.0.0.1:8780?a=1'])
    assert.equal(backendUrlOk(u), false, u);
});
test('externalUrlOk: http(s) and mailto only', () => {
  assert.equal(externalUrlOk('https://example.com/a'), true);
  assert.equal(externalUrlOk('mailto:a@b.c'), true);
  for (const u of ['file:///etc/passwd', 'javascript:alert(1)', 'app://harness/x', 'smb://host/share', 'not a url']) assert.equal(externalUrlOk(u), false, u);
});
test('appUrlToFile stays inside the base', () => {
  assert.equal(appUrlToFile('app://harness/', '/b'), '/b/index.html');
  assert.equal(appUrlToFile('app://harness/assets/x.js', '/b'), '/b/assets/x.js');
  // the URL parser folds %2e%2e; whatever comes out stays inside the base
  const t = appUrlToFile('app://harness/%2e%2e/%2e%2e/etc/passwd', '/b');
  assert.ok(t === null || t.startsWith('/b/'), String(t));
  const t2 = appUrlToFile('app://harness/..%2f..%2fetc/passwd', '/b');
  assert.ok(t2 === null || t2.startsWith('/b/'), String(t2));
  assert.equal(appUrlToFile('app://other/x', '/b'), null);
});
test('webviewSrcOk: only allowed origins', () => {
  assert.equal(webviewSrcOk('http://127.0.0.1:8780/queue', ['http://127.0.0.1:8780']), true);
  assert.equal(webviewSrcOk('http://127.0.0.1:9999/', ['http://127.0.0.1:8780']), false);
  assert.equal(webviewSrcOk('https://evil.example/', ['http://127.0.0.1:8780']), false);
});
test('CSP has no unsafe-eval and no remote script sources', () => {
  assert.ok(!CSP.includes('unsafe-eval'));
  assert.match(CSP, /script-src 'self';/);
  assert.match(CSP, /connect-src 'self'/);
});
