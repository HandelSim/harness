"""Page memory for harness serve's run viewer pages: what is typed or picked in a
page's forms waits in the browser (localStorage) until it is sent, so a closed tab,
a crash or a restart loses none of it. pub.py's send_runs puts js(code, post) in
every page's script, before COMMON_JS.

One draft per form, keyed by the form's action, id and hidden fields (a plan's sig:
a draft from another plan round is never put into this one) and its place among
forms like it; fields outside any form get one draft per page. A draft keeps each
changed field with the value the page first gave it; it is put back only into a
field that still has that value, so what the server shows as changed (a refused form
sent back with what was typed, a setting changed since) wins. Never kept: password,
hidden and file fields, GET forms (their state is in the URL), the sign-in form,
fields named like a secret, anything under data-nomem.

A draft goes once what it was typed for is sent: the page that answers the send
(a ?done= redirect, a page answering the POST itself, or the same page again) with
no error. A refused send keeps it, so Back finds everything. Drafts older than 30
days, and the oldest past about 2 MB in all, are dropped."""

# The status of the page and whether it answers a POST come from the server (js()).
# It runs before COMMON_JS so it still sees ?done= (COMMON_JS drops it from the
# address bar); it puts drafts back once the page's own scripts have run
# (DOMContentLoaded), firing input and change so they catch up, and again into
# fields added later (a live region swapped in, the Add form's repositories).
MEMORY_JS = r"""
(function () {
  var CODE = %CODE%, POST = %POST%, SEARCH = location.search;
  var P = 'hmem:', PEND = 'hmem:pending', KEEP = 30 * 864e5, CAP = 2000000, WAIT = 600000;
  var LIVE = '[id^="r-"], #side, #msgs', FIELDS = 'input, textarea, select';
  var SECRET = /^(key|token|secret|password|passwd|pass|pwd|cookie|credentials?|apikey|auth)$/i;
  var ls, ss, timer = null, dirty = [], loose = false, quiet = false, scanT = null;
  try { ls = window.localStorage; ls.getItem(PEND); } catch (e) { return; }
  try { ss = window.sessionStorage; ss.getItem(PEND); } catch (e) { ss = ls; }
  function safe(fn) { return function (a) { try { fn(a); } catch (e) {} }; }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function isArr(a) { return Object.prototype.toString.call(a) === '[object Array]'; }

  // --- storage: every access may throw (private mode, quota, blocked)
  function read(k) {
    try {
      var d = JSON.parse(ls.getItem(k) || 'null');
      return d && typeof d === 'object' && d.f && typeof d.f === 'object' ? d : null;
    } catch (e) { return null; }
  }
  function drop(k) { try { ls.removeItem(k); } catch (e) {} }
  function write(k, d) {
    var s = JSON.stringify(d);
    try { ls.setItem(k, s); } catch (e) {
      prune(k.length + s.length, k);
      try { ls.setItem(k, s); } catch (e2) {}
    }
  }
  // Drafts past KEEP go; then the oldest, while all of them are over CAP.
  function prune(need, keep) {
    var all = [], total = need, now = Date.now(), i, k, v, t;
    for (i = 0; i < ls.length; i++) {
      k = ls.key(i);
      if (!k || k.indexOf(P) !== 0 || k === PEND || k === keep) continue;
      v = ls.getItem(k) || '';
      t = 0;
      try { t = +JSON.parse(v).t || 0; } catch (e) {}
      all.push([t, k, k.length + v.length]);
      total += k.length + v.length;
    }
    all.sort(function (a, b) { return a[0] - b[0]; });
    all.forEach(function (x) {
      if (now - x[0] > KEEP || total > CAP) { drop(x[1]); total -= x[2]; }
    });
  }

  // --- the send this page answers: its drafts go when it went through
  (function () {
    var p = null, nav = null;
    try { p = JSON.parse(ss.getItem(PEND) || 'null'); ss.removeItem(PEND); } catch (e) {}
    if (!p || !p.k || !(Date.now() - p.t < WAIT)) return;
    try { nav = performance.getEntriesByType('navigation')[0]; } catch (e) {}
    if ((nav && nav.type || 'navigate') !== 'navigate' || CODE >= 400 || /[?&]bad=/.test(SEARCH)) return;
    if (/[?&]done=/.test(SEARCH) || POST || location.pathname === p.p)
      [].concat(p.k).forEach(function (k) { if (typeof k === 'string' && k.indexOf(P) === 0) drop(k); });
  })();

  // --- which forms and fields are kept
  function formOk(f) {
    if (!f || f.closest('[data-nomem]')) return false;
    if ((f.getAttribute('method') || 'get').toLowerCase() !== 'post') return false;
    var a = f.getAttribute('action') || '';
    return a !== '/login' && a !== '/logout';
  }
  function fieldOk(x) {
    if (/^(password|hidden|file|submit|button|reset|image)$/i.test(x.type || '')) return false;
    var name = x.name || x.id;
    if (!name || x.closest('[data-nomem]')) return false;
    if (/password|one-time-code/.test(x.getAttribute('autocomplete') || '')) return false;
    var words = name.split(':')[0].split(/[^A-Za-z0-9]+/);   // repo_mode:<path>: the path is not the name
    for (var i = 0; i < words.length; i++) if (SECRET.test(words[i])) return false;
    var a = x.form ? x.form.getAttribute('action') || '' : '';
    return !(/code/i.test(name) && (a === '/login' || a === '/settings/access'));
  }
  function fieldsOf(f) {   // f: a form, or null for the fields outside any form (not in a live region)
    var out = [];
    (f || document).querySelectorAll(FIELDS).forEach(function (x) {
      if (f ? x.form !== f : x.form || x.closest(LIVE)) return;
      if (fieldOk(x)) out.push(x);
    });
    return out;
  }

  // --- values: what a field holds, and what the page first gave it (hmB)
  function val(x) {
    if (x.type === 'checkbox' || x.type === 'radio') return x.checked;
    if (x.tagName === 'SELECT' && x.multiple) {
      var v = [];
      for (var i = 0; i < x.options.length; i++) if (x.options[i].selected) v.push(x.options[i].value);
      return v;
    }
    return x.value;
  }
  function served(x) {   // as the server wrote it
    if (x.type === 'checkbox' || x.type === 'radio') return x.defaultChecked;
    if (x.tagName !== 'SELECT') return x.defaultValue;
    var v = [], first = null;
    for (var i = 0; i < x.options.length; i++) {
      if (x.options[i].defaultSelected) v.push(x.options[i].value);
      if (first === null && !x.options[i].disabled) first = x.options[i].value;
    }
    return x.multiple ? v : v.length ? v[v.length - 1] : first === null ? '' : first;
  }
  // A field on the page as served: the server's value (the browser may have put back
  // what was typed already); one added later (a swapped region, one a script built):
  // the value it came with.
  function see(x, first) { if (!('hmB' in x)) x.hmB = first ? served(x) : val(x); }
  // The fields of a scope by id: name and place among same-named ones; a radio group
  // by name (its value: the checked one's, or ''); a checkbox by name and value.
  function items(f, first) {
    var out = {}, n = {};
    fieldsOf(f).forEach(function (x) {
      see(x, first);
      var id, e, nm;
      if (x.type === 'radio') {
        if (!x.name) return;
        e = out['r:' + x.name] || (out['r:' + x.name] = {els: [], v: '', b: ''});
        e.els.push(x);
        if (x.checked) e.v = x.value;
        if (x.hmB) e.b = x.value;
        return;
      }
      nm = (x.type === 'checkbox' ? 'c:' : 'v:') + (x.name || '#' + x.id) + (x.type === 'checkbox' ? '=' + x.value : '');
      n[nm] = (n[nm] || 0) + 1;
      id = nm + '#' + n[nm];
      out[id] = {els: [x], v: val(x), b: x.hmB};
    });
    return out;
  }

  // --- a form's draft key: data-mem, else its action, id and hidden fields, and its
  // place among forms like it (as hSwap matches forms); fixed when first seen
  function baseKey(f) {
    if (f.hmBase) return f.hmBase;
    var s = '', h = 0, i;
    f.querySelectorAll('input[type=hidden]').forEach(function (x) { if (x.form === f) s += x.name + '=' + x.value + '\n'; });
    for (i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return (f.hmBase = (f.getAttribute('action') || location.pathname) + '|' + (f.id || '') + '|' + (h >>> 0).toString(36));
  }
  function keyOf(f) {
    if (!f) return P + 'loose|' + location.pathname;
    if (f.hmK) return f.hmK;
    if (f.getAttribute('data-mem')) return (f.hmK = P + 'm|' + f.getAttribute('data-mem'));
    var b = baseKey(f), n = 0, all = document.querySelectorAll('form');
    for (var i = 0; i < all.length; i++) {
      if (baseKey(all[i]) === b) n++;
      if (all[i] === f) break;
    }
    return (f.hmK = P + b + '#' + n);
  }

  // --- saving: the changed fields; a field not on the page now keeps its draft (the
  // Add form's repositories come and go). On a page answering a send the fields show
  // what was sent: a field's draft keeps the value it had before.
  function save(f) {
    var k = keyOf(f), d = read(k), old = d ? d.f : {}, nf = {}, es = items(f, false), id, e, o, b, any = false;
    for (id in old) if (!(id in es) && isArr(old[id])) { nf[id] = old[id]; any = true; }
    for (id in es) {
      e = es[id];
      o = isArr(old[id]) ? old[id] : null;
      b = POST && o ? o[1] : e.b;
      if (!same(e.v, b)) { nf[id] = [e.v, b]; any = true; }
    }
    if (!any) { if (d) drop(k); return; }
    if (d && same(nf, old)) return;
    write(k, {t: Date.now(), u: location.pathname, f: nf});
  }
  function mark(t) {
    if (quiet || !t) return;
    var f = /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName) ? t.form : t.closest ? t.closest('form') : null;
    if (f) { if (!formOk(f)) return; if (dirty.indexOf(f) < 0) dirty.push(f); } else loose = true;
    if (!timer) timer = setTimeout(safe(flush), 300);
  }
  function flush() {
    clearTimeout(timer);
    timer = null;
    var fs = dirty;
    dirty = [];
    fs.forEach(function (f) { if (document.contains(f)) save(f); });
    if (loose) { loose = false; save(null); }
  }

  // --- restoring: into a field seen for the first time that still holds what the page
  // gave it, when the draft was made from that same value
  function setTo(e, v) {
    var x = e.els[0], i, on = null, ok = false;
    if (x.type === 'radio') {
      if (typeof v !== 'string') return null;
      e.els.forEach(function (r) { if (r.value === v) on = r; });
      if (!on && v) return null;
      e.els.forEach(function (r) { r.checked = r === on; });
      return on || x;
    }
    if (x.type === 'checkbox') {
      if (typeof v !== 'boolean') return null;
      x.checked = v;
      return x;
    }
    if (x.tagName === 'SELECT' && x.multiple) {
      if (!isArr(v)) return null;
      for (i = 0; i < x.options.length; i++) x.options[i].selected = v.indexOf(x.options[i].value) >= 0;
      return x;
    }
    if (typeof v !== 'string') return null;
    if (x.tagName === 'SELECT') {
      for (i = 0; i < x.options.length; i++) if (x.options[i].value === v) ok = true;
      if (!ok) return null;
    }
    x.value = v;
    return x;
  }
  function fire(x, type) {
    var ev;
    try { ev = new Event(type, {bubbles: true}); } catch (e) { ev = document.createEvent('Event'); ev.initEvent(type, true, false); }
    x.dispatchEvent(ev);
  }
  // Tell the page's scripts (sync, panels, growing text boxes), keeping the focus and
  // the scroll where they were.
  function tell(hit) {
    var a = document.activeElement, sx = window.scrollX, sy = window.scrollY;
    quiet = true;
    try { hit.forEach(function (x) { fire(x, 'input'); fire(x, 'change'); }); } finally { quiet = false; }
    if (document.activeElement !== a) {
      if (a && a !== document.body && a.focus) a.focus({preventScroll: true});
      else if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    }
    if (window.scrollX !== sx || window.scrollY !== sy) window.scrollTo(sx, sy);
  }
  function restore(f) {
    var k = keyOf(f), es = items(f, false), d, hit = [], id, e, s, x;
    for (id in es) {
      if (es[id].els[0].hmDone) { delete es[id]; continue; }
      es[id].els.forEach(function (y) { y.hmDone = 1; });
    }
    d = read(k);
    if (!d) return;
    for (id in es) {
      e = es[id];
      s = d.f[id];
      if (!isArr(s) || s.length !== 2 || !same(e.v, e.b) || !same(s[1], e.b) || same(s[0], e.v)) continue;
      x = setTo(e, s[0]);
      if (x) hit.push(x);
    }
    if (!hit.length) return;
    hit.forEach(function (y) {   // a folded part (edge cases) opens to show it
      for (var p = y.parentNode; p; p = p.parentNode) if (p.tagName === 'DETAILS' && !p.open) p.open = true;
    });
    tell(hit);
    cue(f, hit[0], d.t);
  }
  // One line at the top of the form: it was put back, and a way to clear it.
  function cue(f, x, t) {
    var host = f || x, d, b, when = '';
    if (host.hmCue && document.contains(host.hmCue)) return;
    try { when = new Date(t).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'}); } catch (e) {}
    d = document.createElement('div');
    d.className = 'muted hmem';
    d.textContent = 'Put back what you typed here before' + (when ? ' (' + when + ')' : '') + '. ';
    b = d.appendChild(document.createElement('button'));
    b.type = 'button';
    b.className = 'btn sm';
    b.textContent = 'Clear it';
    b.title = 'Back to the form as the page gave it';
    b.addEventListener('click', safe(function () {
      if (!window.confirm('Clear what you typed in this form? It cannot be brought back.')) return;
      var es = items(f, false), hit = [], id, y;
      for (id in es) if (!same(es[id].v, es[id].b) && (y = setTo(es[id], es[id].b))) hit.push(y);
      tell(hit);
      drop(keyOf(f));
      if (d.parentNode) d.parentNode.removeChild(d);
    }));
    if (f) f.insertBefore(d, f.firstChild);
    else x.parentNode.insertBefore(d, x);
    host.hmCue = d;
  }
  function restoreAll() {
    document.querySelectorAll('form').forEach(function (f) { if (formOk(f)) restore(f); });
    restore(null);
  }
  function later() {
    if (!scanT) scanT = setTimeout(function () { scanT = null; safe(restoreAll)(); }, 50);
  }
  function start() {
    restoreAll();
    if (window.MutationObserver) new MutationObserver(safe(function (ms) {
      for (var i = 0; i < ms.length; i++) for (var j = 0; j < ms[i].addedNodes.length; j++) {
        var n = ms[i].addedNodes[j];
        if (n.nodeType === 1 && (n.matches(FIELDS) || n.querySelector(FIELDS))) { later(); return; }
      }
    })).observe(document.body, {childList: true, subtree: true});
    setTimeout(safe(function () { prune(0, ''); }), 2000);
  }

  // The fields as served, before any script changes them.
  document.querySelectorAll('form').forEach(function (f) { if (formOk(f)) { keyOf(f); items(f, true); } });
  items(null, true);
  document.addEventListener('input', safe(function (e) { mark(e.target); }), true);
  document.addEventListener('change', safe(function (e) { mark(e.target); }), true);
  document.addEventListener('click', safe(function (e) {   // a button a script fills or empties fields with
    var b = e.target.closest && e.target.closest('button, [data-mem-clear]');
    if (!b) return;
    if (b.hasAttribute('data-mem-clear')) {
      var f = b.form || b.closest('form');
      setTimeout(safe(function () { drop(keyOf(f && formOk(f) ? f : null)); }), 0);
      return;
    }
    mark(b);
  }), true);
  document.addEventListener('reset', safe(function (e) {
    var f = e.target;
    if (formOk(f)) setTimeout(safe(function () { save(f); }), 0);
  }), true);
  // Sent: its draft goes once the answer says it went through. Bubbling, after
  // COMMON_JS's data-confirm (capturing) had its say.
  document.addEventListener('submit', safe(function (e) {
    var f = e.target;
    if (e.defaultPrevented || !formOk(f)) return;
    flush();
    save(f);
    try { ss.setItem(PEND, JSON.stringify({k: [keyOf(f)], p: location.pathname, t: Date.now()})); } catch (x) {}
  }));
  window.addEventListener('pagehide', safe(flush));
  document.addEventListener('visibilitychange', safe(function () { if (document.hidden) flush(); }));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', safe(start));
  else setTimeout(safe(start), 0);
})();
"""


def js(code, post=False):
    """MEMORY_JS for a page served with HTTP status code, post: it answers a POST."""
    return MEMORY_JS.replace("%CODE%", str(int(code))).replace("%POST%", "1" if post else "0")
