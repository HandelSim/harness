"""harness serve's Changes page for a queue ticket: a diff, drawn in the browser by
the vendored diff2html (vendor/README.md), of the repository picked from a list:
the project's own, or for a ticket that works in the project folder any repository
inside it (its own .git, not a submodule; the queue's nested_repos).

Three views: the ticket's work in that repository (its part of what
ticket_queue.ticket_change takes for the change, what review, fix and the verifier
read: its branch since the commit it started from, or for a part left uncommitted
the files as they are against the snapshot taken when the work started), the
uncommitted changes, and the commits not pushed yet. Read-only git (no fetch, no
optional locks, no external diff or textconv programs), but for a part left
uncommitted the queue's worktree_tree writes the files as they are into that
repository's objects, through an index of its own, as review does. Files that look
like secrets are left out of every diff (vault_view.SECRET).

On the work view of a ticket the queue would take them for (harness queue fix), in
any repository of the ticket's, and on the uncommitted view where its files are the
ones fix works on, the owner clicks lines to comment on them, PR-review style; the
comments wait in the browser (localStorage) and go to the fix stage all at once, one
on a repository inside the project with its "repo", one on the uncommitted view with
"view" (its old lines are numbered as at HEAD)."""

import json
import os
import re
import subprocess
import threading
from urllib.parse import quote

import runs as runs_view
from vault_view import SECRET

esc = runs_view.esc
HERE = os.path.dirname(os.path.abspath(__file__))
DIFF_CAP = 2_000_000       # bytes of diff sent to the page
NEW_FILES_CAP = 100        # untracked files shown as new
NEW_FILE_CAP = 200_000     # bytes of one untracked file shown
LOG_CAP = 50               # commits listed
GIT_TIMEOUT = 20           # s
VIEWS = {"work": "The ticket's work", "uncommitted": "Uncommitted changes", "unpushed": "Not pushed yet"}
VIEW_SAYS = {"work": "the ticket's branch since the commit it started from: what review reads and push sends",
             "left": ("this repository's part of the ticket is left uncommitted: the files as they are now (new "
                      "files too) against the snapshot taken when the work started; what review, fix and the "
                      "verifier read"),
             "uncommitted": "what is changed in the folder and not committed, new files included",
             "unpushed": "the commits on the branch the folder is on that its upstream does not have"}


def _git(d, *args, cap=None):
    """Read-only git in d: (stdout text, cut short at cap bytes, DIFF_CAP by default) or
    (None, False) when it fails."""
    cap = cap or DIFF_CAP
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    cmd = ["git", "--no-pager", "-C", d, "-c", "core.fsmonitor=false", "-c", "color.ui=false",
           "-c", "core.quotepath=false", "-c", "diff.noprefix=false", *args]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=env, **runs_view.NO_WINDOW)
    except OSError:
        return None, False
    timer = threading.Timer(GIT_TIMEOUT, p.kill)
    timer.start()
    try:
        out = p.stdout.read(cap + 1)
        cut = len(out) > cap
        if cut:
            p.kill()
        p.stdout.close()
        p.wait()
    finally:
        timer.cancel()
    if not cut and p.returncode != 0:
        return None, False
    return out[:cap].decode("utf-8", "replace"), cut


DIFF = ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M")


def _state(runs, key):
    """The ticket's state.json as the queue wrote it, or None."""
    if not runs.queue_dir or not runs_view.KEY_RE.match(key or "") or key not in runs._ticket_dirs():
        return None
    try:
        with open(os.path.join(runs.queue_dir, key, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and isinstance(st.get("project"), str) else None


def repos(runs, st):
    """[(value, label)]: the project's repository, then the ones inside it when the
    ticket works in the project folder (a copy holds only the project's own), and any
    other one with a part of the ticket's change (a submodule of one left uncommitted)."""
    out = [(".", "the project itself (" + (os.path.basename(st["project"].rstrip("/\\")) or st["project"]) + ")")]
    tq = runs_view._tq()
    if st.get("in_place") is True and tq and os.path.isdir(st["project"]):
        try:
            out += [(r, r) for r in tq.nested_repos(st["project"], skip=(runs.queue_dir,))]
        except Exception:   # an odd folder: the project's own still shows
            pass
        have = {r for r, _ in out}
        out += [(r, r) for r in sorted(set(_dict(st, "nested")) | set(_dict(st, "baselines")))
                if r not in have and isinstance(r, str) and os.path.isdir(os.path.join(st["project"], r))]
    return out


def _dict(st, k):
    return st.get(k) if isinstance(st.get(k), dict) else {}


def part(st, repo):
    """What the ticket's change is in repo, as ticket_queue.ticket_change takes it:
    ("branch", branch, base) for the top on its branch or one inside the project given a
    branch (--repo-branch, st["nested"]), ("left", snapshot, "") for one left uncommitted
    (the top with --no-branch, the others inside it) once its snapshot is taken
    (st["baselines"]), else None."""
    def base(d):
        return d.get("base_sha") if isinstance(d.get("base_sha"), str) else ""
    if repo == "." and not st.get("no_branch"):
        return "branch", st.get("branch") or st["key"], base(st)
    n = _dict(st, "nested").get(repo) if repo != "." else None
    if isinstance(n, dict) and isinstance(n.get("branch"), str) and n["branch"]:
        return "branch", n["branch"], base(n)
    b = _dict(st, "baselines").get(repo)
    if isinstance(b, dict) and isinstance(b.get("snap"), str) and b["snap"]:
        return "left", b["snap"], ""
    return None


def left_uncommitted(st, repo):
    """repo's part of the ticket is (or will be) left uncommitted: it has no branch of the ticket's."""
    if repo == ".":
        return bool(st.get("no_branch"))
    return repo in _dict(st, "baselines") or (repo not in _dict(st, "nested") and repo not in _dict(st, "repos"))


def has_work_view(st, repo):
    """The work view is there for the top, and for a repository inside the project with
    a part of the change (or given a branch, before the work started)."""
    return repo == "." or part(st, repo) is not None or repo in _dict(st, "repos")


def _secret_free(diff):
    """diff without the files that look like secrets; (diff, their names)."""
    keep, hid = [], []
    for block in re.split(r"(?m)^(?=diff --git )", diff):
        m = re.match(r"diff --git a/(.*) b/(.*)", block)
        if m and (SECRET.search(m.group(1)) or SECRET.search(m.group(2))):
            hid.append(m.group(2))
        else:
            keep.append(block)
    return "".join(keep), hid


def _new_files(d, room):
    """The untracked (not ignored) files in d as new-file diffs; (diff, notes)."""
    out, notes = [], []
    raw, _ = _git(d, "ls-files", "-z", "--others", "--exclude-standard", cap=200_000)
    names = [n for n in (raw or "").split("\0") if n and not SECRET.search(n)]
    if len(names) > NEW_FILES_CAP:
        notes.append(f"{len(names)} new files; the first {NEW_FILES_CAP} are shown")
    for rel in names[:NEW_FILES_CAP]:
        p = os.path.join(d, rel)
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                data = f.read(NEW_FILE_CAP + 1)
        except OSError:
            continue
        head = f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n"
        if b"\0" in data[:8000]:
            out.append(head + f"Binary files /dev/null and b/{rel} differ\n")
        else:
            if len(data) > NEW_FILE_CAP:
                notes.append(f"{rel} is over {NEW_FILE_CAP // 1000} kB; its start is shown")
            lines = data[:NEW_FILE_CAP].decode("utf-8", "replace").splitlines()
            out.append(head + f"--- /dev/null\n+++ b/{rel}\n" + (f"@@ -0,0 +1,{len(lines)} @@\n" if lines else "")
                       + "".join("+" + x + "\n" for x in lines))
        room -= len(out[-1])
        if room < 0:
            notes.append("the new files are cut short: the diff is too big to show in full")
            out.pop()
            break
    return "".join(out), notes


def _log(d, rng):
    raw, _ = _git(d, "log", "-n", str(LOG_CAP), "--format=%h%x09%s", rng, "--", cap=100_000)
    return [ln.split("\t", 1) for ln in (raw or "").splitlines() if "\t" in ln]


def folder(runs, st, repo):
    """The folder repo's files are in: the project (or one inside it) when the ticket works
    in place, else its separate copy; None when the copy is gone."""
    key, project = st["key"] if isinstance(st.get("key"), str) else "", st["project"]
    if repo != ".":
        return os.path.join(project, repo)
    clone = os.path.join(runs.queue_dir, key, "repo")
    return project if st.get("in_place") is True else clone if os.path.isdir(clone) else None


def uncommitted_takes_comments(runs, st, repo):
    """(yes, why not): the uncommitted view of repo takes review comments when its files
    are the ones fix works on: the part of the ticket's left uncommitted there, or the
    folder on the ticket's branch there (its leftovers, which fix may commit). Not when
    the repository has no part, nor when the folder is on another branch (the queue put
    it back on the owner's when the ticket stopped): those files are not the ticket's."""
    got = part(st, repo)
    if got is None:
        return False, ""
    if got[0] == "left":
        return True, ""
    d = folder(runs, st, repo)
    on = (_git(d, "symbolic-ref", "-q", "--short", "HEAD", cap=500)[0] or "").strip() if d and os.path.isdir(d) else ""
    if on == got[1]:
        return True, ""
    return False, f"the folder is on {on or 'a detached HEAD'}, not the ticket's branch {got[1]}"


def changes(runs, st, repo, view):
    """{"diff", "says", "notes", "commits", "error"} for one repository and view."""
    key, project = st["key"] if isinstance(st.get("key"), str) else "", st["project"]
    d = folder(runs, st, repo)
    res = {"diff": "", "says": "", "notes": [], "commits": [], "error": "", "kind": ""}
    if view == "work":
        got = part(st, repo)
        res["kind"] = got[0] if got else ""
        if not got or not got[1] or (got[0] == "branch" and not got[2]):
            res["error"] = "the ticket has not started yet, so it has no work to show" + (
                " here: the snapshot its change here is compared with is taken before the first stage after the plan"
                if st.get("base_sha") and left_uncommitted(st, repo) else "")
            return res
        if got[0] == "left":
            tq = runs_view._tq()
            if tq is None:
                res["error"] = ("the queue's own module could not be loaded, so these files cannot be read "
                                "as review reads them")
                return res
            if not d or not os.path.isdir(d):
                res["error"] = "the repository is gone from the project folder"
                return res
            try:   # the files as they are, as review takes them: the queue's own code, not a copy of it
                tree = tq.worktree_tree(tq.Queue(runs.queue_dir), st, d)
            except Exception:   # noqa: BLE001  git failed, or a folder git will not read
                tree = ""
            if not tree:
                res["error"] = "git could not read this repository's files"
                return res
            res["says"] = f"{got[1][:12]} (refs/harness-queue/base/{key}) and the files in the folder"
            res["diff"], cut = _git(d, *DIFF, got[1], tree, "--")
        else:
            branch, base = got[1], got[2]
            if d and _git(d, "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", cap=200)[0]:
                where, tip = d, f"refs/heads/{branch}"
            elif repo == "." and _git(project, "rev-parse", "-q", "--verify",
                                      f"refs/harness-queue/{key}^{{commit}}", cap=200)[0]:
                where, tip = project, f"refs/harness-queue/{key}"
            else:
                res["error"] = f"the branch {branch} is not there any more"
                return res
            res["says"] = f"{base[:12]}..{branch}"
            res["commits"] = _log(where, f"{base}..{tip}")
            res["diff"], cut = _git(where, *DIFF, base, tip, "--")
    else:
        if not d or not os.path.isdir(d):
            res["error"] = "the separate copy is gone (the ticket finished or was reset), so there is nothing to show"
            return res
        if not _git(d, "rev-parse", "-q", "--verify", "HEAD^{commit}", cap=200)[0]:
            res["error"] = "this repository has no commits yet"
            return res
        if view == "uncommitted":
            res["says"] = "HEAD and the files in the folder"
            res["diff"], cut = _git(d, *DIFF, "HEAD", "--")
            if res["diff"] is not None and not cut:
                more, notes = _new_files(d, DIFF_CAP - len(res["diff"]))
                res["diff"] += more
                res["notes"] += notes
        else:
            up = (_git(d, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", cap=500)[0] or "").strip()
            if not up:
                res["error"] = ("the branch this repository is on has no upstream (it was never pushed, or has no "
                                "remote), so there is nothing to compare with")
                return res
            res["says"] = f"{up}...HEAD, as of the last fetch"
            res["commits"] = _log(d, f"{up}..HEAD")
            res["diff"], cut = _git(d, *DIFF, f"{up}...HEAD", "--")
    if res["diff"] is None:
        res["diff"], res["error"] = "", "git could not read this repository"
        return res
    if cut:
        res["diff"] = res["diff"][:res["diff"].rfind("\ndiff --git ") + 1] or res["diff"]
        res["notes"].insert(0, f"the diff is too big to show in full ({DIFF_CAP // 1000} kB at most); "
                                "the files after the cut are not shown")
    res["diff"], hid = _secret_free(res["diff"])
    if hid:
        res["notes"].append("left out because they look like secrets: " + ", ".join(hid[:10]))
    return res


def _asset(name):
    with open(os.path.join(HERE, "vendor", "diff2html", name), encoding="utf-8") as f:
        return f.read()


STYLE = """
.dv-pick{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:end}
.dv-pick label{display:flex;flex-direction:column;gap:4px;font-size:13px}
.dv-pick label{min-width:0;max-width:100%}
.dv-pick select{width:100%;min-width:0}
.dv-repo{flex:1 1 320px}
.dv-views label{flex-direction:row;align-items:center;gap:6px}
.dv-views label.off{opacity:.5;cursor:not-allowed}
.dv-lay{display:flex;gap:6px;margin:8px 0}
.dv-lay button[aria-pressed=true]{font-weight:600;border-color:var(--accent)}
.dv-commits{margin:6px 0 0;padding-left:18px;font-size:13px}
#d2h{overflow-x:auto}
/* the page takes the window's width, and a long line wraps in its column instead of
   running past it (diff2html's own rule keeps each line on one row and scrolls sideways) */
main.rv:has(#d2h){max-width:none}
body:has(#d2h) .bar-in{max-width:none}
#d2h .d2h-file-side-diff{overflow-x:auto}
/* diff2html sizes a code line as calc(100% - its padding), which holds only for content-box;
   the page's border-box rule took the padding off twice (16em a line in one column) */
#d2h .d2h-code-line,#d2h .d2h-code-side-line{box-sizing:content-box}
#d2h .d2h-code-line-ctn{white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top}
#d2h .d2h-code-line-ctn ins,#d2h .d2h-code-line-ctn del{display:inline}
/* the page's own table rules (right-aligned, padded cells) undone for diff2html's */
#d2h td,#d2h th{text-align:left;padding:0;border-bottom:0;vertical-align:top}
#d2h td.d2h-code-linenumber,#d2h td.d2h-code-side-linenumber{text-align:right}
#d2h td.d2h-code-side-linenumber{padding:0 .5em}
#d2h .d2h-file-list-wrapper{margin-bottom:12px}
#d2h.dv-can td.d2h-cntx,#d2h.dv-can td.d2h-ins,#d2h.dv-can td.d2h-del{cursor:pointer}
#d2h tr.dv-c td{padding:8px 10px;background:var(--raised);border-left:3px solid var(--accent);white-space:normal;
  font-family:var(--sans);font-size:13px}
#d2h tr.dv-pad td{padding:0;border-left:0}
.dv-note{white-space:pre-wrap;overflow-wrap:anywhere}
.dv-ed textarea,#dv-gen{width:100%;box-sizing:border-box;min-height:64px;font:inherit;color:var(--ink);
  background:var(--field);border:1px solid var(--line-2);border-radius:6px;padding:6px 8px}
.dv-row{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.dv-list{padding-left:20px}
.dv-list li{margin:8px 0}
.dv-list .txt{white-space:pre-wrap;overflow-wrap:anywhere}
"""

JS = r"""
(function () {
  var el = document.getElementById('d2h'), src = document.getElementById('dv-data');
  var pick = document.getElementById('dv-pick');
  if (pick) pick.addEventListener('change', function () { pick.submit(); });
  if (!el || !src || !window.Diff2Html) return;
  var diff = JSON.parse(src.textContent), key = 'harness.diff.layout', lay = '';
  try { lay = localStorage.getItem(key) || ''; } catch (e) {}
  if (!lay) lay = window.innerWidth < 900 ? 'line-by-line' : 'side-by-side';
  function dark() {
    var t = document.documentElement.getAttribute('data-theme');
    return t ? t === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function draw() {
    el.innerHTML = Diff2Html.html(diff, {drawFileList: true, outputFormat: lay, matching: 'lines',
                                         colorScheme: dark() ? 'dark' : 'light'});
    document.querySelectorAll('.dv-lay button').forEach(function (b) {
      b.setAttribute('aria-pressed', b.dataset.lay === lay ? 'true' : 'false');
    });
    level();
    if (rv) rv();
  }
  // Side by side, each half is its own table, so a line that wraps in one half makes its row
  // taller than the row level with it in the other: each such pair is set to the taller height.
  // Every height is cleared, then all are read, then set, so the page lays out once.
  function code(tr) { return !tr.classList.contains('dv-c'); }
  function level() {
    var sets = [];
    el.querySelectorAll('.d2h-files-diff').forEach(function (fd) {
      var h = fd.querySelectorAll('.d2h-file-side-diff');
      if (h.length !== 2) return;
      sets.push([0, 1].map(function (i) { return [].filter.call(h[i].querySelectorAll('tbody > tr'), code); }));
    });
    sets.forEach(function (s) { s[0].concat(s[1]).forEach(function (tr) { tr.style.height = ''; }); });
    var tall = sets.map(function (s) {
      return s.map(function (rows) { return rows.map(function (tr) { return tr.getBoundingClientRect().height; }); });
    });
    sets.forEach(function (s, k) {
      var a = tall[k][0], b = tall[k][1];
      for (var i = 0; i < Math.min(a.length, b.length); i++) {
        if (a[i] < b[i] - 0.5) s[0][i].style.height = b[i] + 'px';
        else if (b[i] < a[i] - 0.5) s[1][i].style.height = a[i] + 'px';
      }
    });
  }
  var leveling = 0;
  window.addEventListener('resize', function () {
    if (leveling) return;
    leveling = requestAnimationFrame(function () { leveling = 0; level(); });
  });
  var box = document.querySelector('[data-sent]'), rv = box && reviewer(box);

  // Review comments: a click on a line opens a box under it; the comments wait in
  // localStorage (dropped once the ticket's sent batches change: they were sent) and
  // the form sends them all as JSON. Everything shown is set as text, never as HTML.
  function reviewer(box) {
    var sent = box.dataset.sent, store = 'harness.review.' + box.dataset.key, got = null;
    var repo = box.dataset.repo || '.';   // the repository this diff is of; a draft's comments can be on others
    var view = box.dataset.view === 'uncommitted' ? 'uncommitted' : 'work';   // and the view: its line numbers
    try {
      got = JSON.parse(localStorage.getItem(store) || 'null');
      if (got && got.sent !== sent) { localStorage.removeItem(store); got = null; }
    } catch (e) {}
    if (box.id !== 'dv-review') return null;   // the queue would not take them now: the draft waits
    var max = +box.dataset.max, tmax = +box.dataset.textMax;
    var list = document.getElementById('dv-list'), gen = document.getElementById('dv-gen');
    var add = document.getElementById('dv-add'), form = document.getElementById('dv-send');
    var send = form.querySelector('button'), full = 'At most ' + max + ' comments go to one fix: send these, ' +
      'then the rest once it is done.';
    var items = got && Array.isArray(got.items) ? got.items.filter(ok).slice(0, max) : [];
    var open = null, genAt = -1, rows = {}, pairs = [];
    // what was being written when the page went: the open line box (back once its line is drawn) and
    // the box for the whole change
    if (got && got.open && got.open.at && typeof got.open.text === 'string' && typeof got.open.i === 'number' &&
        got.open.i < items.length) open = {i: got.open.i, at: got.open.at, text: got.open.text.slice(0, tmax)};
    if (got && typeof got.gen === 'string' && got.gen && !gen.value) {
      gen.value = got.gen.slice(0, tmax);
      genAt = typeof got.genAt === 'number' && got.genAt >= 0 && got.genAt < items.length ? got.genAt : -1;
      if (genAt >= 0) add.textContent = 'Save comment';
    }
    function ok(c) {
      return !!c && typeof c.text === 'string' && !!c.text.trim() && (!c.path || (typeof c.path === 'string' &&
        c.line > 0 && (c.side === 'new' || c.side === 'old') && typeof c.code === 'string' &&
        (c.repo === undefined || (typeof c.repo === 'string' && !!c.repo)) &&
        (c.view === undefined || c.view === 'uncommitted')));
    }
    function keep() {   // the comments and what is being written, so a closed tab or a restart loses nothing
      var draft = open && open.text.trim() ? {i: open.i, at: open.at, text: open.text} : null, g = gen.value;
      try {
        if (items.length || draft || g.trim()) {
          localStorage.setItem(store, JSON.stringify({sent: sent, items: items, open: draft, gen: g, genAt: genAt}));
        } else localStorage.removeItem(store);
      } catch (e) {}
    }
    function node(tag, cls, text) {
      var n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    }
    function btn(text, fn) {
      var b = node('button', 'btn sm', text);
      b.type = 'button';
      b.addEventListener('click', fn);
      return b;
    }
    function where(c) {
      if (!c.path) return 'the whole change';
      return (c.repo && c.repo !== '.' ? c.repo + '/' : '') + c.path + ':' + c.line +
        (c.side === 'old' ? (c.view === 'uncommitted' ? ' (old, at HEAD)' : ' (old)') : '') +
        (c.view === 'uncommitted' ? ', uncommitted' : '');
    }
    function mine(c) { return (c.repo || '.') === repo && (c.view || 'work') === view; }   // on this diff
    function at(c) { return mine(c) ? rows[c.path + '\n' + c.side + '\n' + c.line] : undefined; }
    function on(c) {   // where a comment was made, when not on the top's work view
      var o = {};
      if (c.repo && c.repo !== '.') o.repo = c.repo;
      if (c.view === 'uncommitted') o.view = c.view;
      return o;
    }
    function put(tr, path, side, line, own) {
      rows[path + '\n' + side + '\n' + line] = rows[path + '\n' + side + '\n' + line] || tr;
      if (own) tr.dvAt = {path: path, side: side, line: line};
    }
    function index() {   // each numbered row of the drawn diff: its file (as Diff2Html.parse names it), side, line
      var files = [];
      rows = {};
      try { files = Diff2Html.parse(diff); } catch (e) {}
      el.querySelectorAll('.d2h-file-wrapper').forEach(function (w, i) {
        var f = files[i];
        if (!f) return;
        var path = f.newName && f.newName !== '/dev/null' ? f.newName : f.oldName;
        var halves = w.querySelectorAll('.d2h-file-side-diff');
        if (halves.length === 2) {
          [['old', halves[0]], ['new', halves[1]]].forEach(function (h) {
            h[1].querySelectorAll('tr').forEach(function (tr) {
              var td = tr.querySelector('td.d2h-code-side-linenumber'), n = td ? parseInt(td.textContent, 10) : 0;
              if (n > 0) put(tr, path, h[0], n, true);
            });
          });
          return;
        }
        w.querySelectorAll('tr').forEach(function (tr) {
          var a = tr.querySelector('.line-num1'), b = tr.querySelector('.line-num2');
          var n1 = a ? parseInt(a.textContent, 10) : 0, n2 = b ? parseInt(b.textContent, 10) : 0;
          if (n2 > 0) put(tr, path, 'new', n2, true);
          if (n1 > 0) put(tr, path, 'old', n1, !(n2 > 0));
        });
      });
    }
    function last(tr) {
      while (tr.nextElementSibling && tr.nextElementSibling.classList.contains('dv-c')) tr = tr.nextElementSibling;
      return tr;
    }
    function plain(tr) { return !tr.classList.contains('dv-c'); }
    function place(tr, content) {   // a row under tr; side by side, an empty one as tall in the other half
      var row = node('tr', 'dv-c'), td = node('td');
      td.colSpan = 2;
      td.appendChild(content);
      row.appendChild(td);
      last(tr).insertAdjacentElement('afterend', row);
      var half = tr.closest('.d2h-file-side-diff');
      if (!half) return;
      var other = half.parentNode.querySelectorAll('.d2h-file-side-diff')[half.previousElementSibling ? 0 : 1];
      var mine = [].filter.call(tr.parentNode.children, plain);
      var twin = other && [].filter.call(other.querySelectorAll('tbody > tr'), plain)[mine.indexOf(tr)];
      if (!twin) return;
      var pad = node('tr', 'dv-c dv-pad'), cell = node('td');
      cell.colSpan = 2;
      pad.appendChild(cell);
      last(twin).insertAdjacentElement('afterend', pad);
      pairs.push([row, pad]);
    }
    function even() {
      pairs.forEach(function (p) { p[1].style.height = p[0].getBoundingClientRect().height + 'px'; });
    }
    function drop(i) {
      items.splice(i, 1);
      if (open && open.i === i) open = null;
      else if (open && open.i > i) open.i--;
      if (genAt === i) { genAt = -1; add.textContent = 'Add comment'; }
      else if (genAt > i) genAt--;
      keep();
      paint();
    }
    function edit(i) {
      var tr = items[i].path && at(items[i]);
      if (tr) {
        open = {i: i, at: items[i], text: items[i].text};
        paint();
        return;
      }
      genAt = i;   // a comment on the whole change, or on a line this diff no longer has
      gen.value = items[i].text;
      add.textContent = 'Save comment';
      gen.focus();
    }
    function save(text) {
      text = text.trim();
      if (!text) return;
      if (open.i >= 0) {
        items[open.i].text = text;
      } else {
        if (items.length >= max) { alert(full); return; }
        items.push(Object.assign(on(open.at), {path: open.at.path, line: open.at.line, side: open.at.side,
                                               code: open.at.code, text: text}));
      }
      open = null;
      keep();
      paint();
    }
    function editor() {
      var w = node('div', 'dv-ed'), t = node('textarea'), acts = node('div', 'dv-row');
      t.maxLength = tmax;
      t.rows = 3;
      t.value = open.text;
      t.setAttribute('aria-label', 'Comment on ' + where(open.at));
      t.addEventListener('input', function () { open.text = t.value; keep(); even(); });
      acts.appendChild(btn(open.i >= 0 ? 'Save' : 'Add comment', function () { save(t.value); }));
      acts.appendChild(btn('Cancel', function () { open = null; keep(); paint(); }));
      w.appendChild(node('div', 'muted', where(open.at)));
      w.appendChild(t);
      w.appendChild(acts);
      setTimeout(function () { t.focus(); }, 0);
      return w;
    }
    function note(c, i) {
      var w = node('div'), acts = node('div', 'dv-row');
      w.appendChild(node('div', 'dv-note', c.text));
      acts.appendChild(btn('Edit', function () { edit(i); }));
      acts.appendChild(btn('Remove', function () { drop(i); }));
      w.appendChild(acts);
      return w;
    }
    function paint() {
      el.querySelectorAll('tr.dv-c').forEach(function (r) { r.parentNode.removeChild(r); });
      pairs = [];
      items.forEach(function (c, i) {
        var tr = c.path && at(c);
        if (tr && !(open && open.i === i)) place(tr, note(c, i));
      });
      if (open && at(open.at)) place(at(open.at), editor());
      else if (open && mine(open.at)) open = null;   // its line is gone; one on another repository waits there
      list.textContent = '';
      items.forEach(function (c, i) {
        var li = node('li'), acts = node('div', 'dv-row');
        var head = li.appendChild(node(c.path && !mine(c) ? 'a' : 'b', '', where(c)));
        if (c.path && !mine(c)) {
          head.href = '?repo=' + encodeURIComponent(c.repo || '.') + '&view=' + (c.view || 'work');
          head.title = 'made on another repository or view of the ticket\'s: go there';
        }
        if (c.code && c.code.trim()) li.appendChild(node('code', '', ' ' + c.code.trim()));
        li.appendChild(node('div', 'txt', c.text));
        acts.appendChild(btn('Edit', function () { edit(i); }));
        acts.appendChild(btn('Remove', function () { drop(i); }));
        li.appendChild(acts);
        list.appendChild(li);
      });
      send.disabled = !items.length;
      send.textContent = 'Send ' + items.length + ' comment' + (items.length === 1 ? '' : 's') + ' to the fixer';
      even();
    }
    gen.addEventListener('input', function () { keep(); });
    el.classList.add('dv-can');
    el.addEventListener('click', function (e) {
      var tr = e.target.closest ? e.target.closest('tr') : null;
      if (!tr || !tr.dvAt || String(window.getSelection ? window.getSelection() : '')) return;
      if (open && open.text.trim() && !confirm('Drop the comment you are writing on ' + where(open.at) + '?')) return;
      if (items.length >= max) { alert(full); return; }
      var ctn = tr.querySelector('.d2h-code-line-ctn'), code = ctn ? ctn.textContent : '';
      open = {i: -1, text: '', at: Object.assign(on({repo: repo, view: view}), {path: tr.dvAt.path, side: tr.dvAt.side,
        line: tr.dvAt.line, code: code.length > 160 ? code.slice(0, 157) + '...' : code})};
      paint();
    });
    add.addEventListener('click', function () {
      var t = gen.value.trim();
      if (!t) return;
      if (genAt >= 0 && items[genAt]) items[genAt].text = t;
      else if (items.length >= max) { alert(full); return; }
      else items.push({text: t});
      genAt = -1;
      gen.value = '';
      add.textContent = 'Add comment';
      keep();
      paint();
    });
    form.addEventListener('submit', function (e) {
      var busy = (open && open.text.trim()) || gen.value.trim();
      if (!items.length || (busy && !confirm('Send without the comment you are still writing?'))) {
        e.preventDefault();
        return;
      }
      form.elements.comments.value = JSON.stringify(items.map(function (c) {
        return c.path ? Object.assign(on(c), {path: c.path, line: c.line, side: c.side, code: c.code, text: c.text})
          : {text: c.text};
      }));
    });
    window.addEventListener('resize', even);
    return function () { index(); paint(); };
  }
  document.querySelectorAll('.dv-lay button').forEach(function (b) {
    b.addEventListener('click', function () {
      lay = b.dataset.lay;
      try { localStorage.setItem(key, lay); } catch (e) {}
      draw();
    });
  });
  draw();
})();
"""


def _review(runs, key, st, repo, view="work", hint_more=""):
    """(the hint over the diff, the panel under it) for review comments on view (work, or
    uncommitted where uncommitted_takes_comments) of repo: the comment box and send button
    when harness queue fix would take them, else why not. data-sent marks the batches sent
    so far: the browser drops a draft made before the last. data-repo and data-view: the
    comments made here are on that repository's files as this view numbers them (the draft
    is one for the ticket, its comments on any repository and view)."""
    why = runs.fix_refused(key)
    if why is None:
        return "", ""
    sent = st.get("owner_reviews") if isinstance(st.get("owner_reviews"), list) else []
    mark = f"{len(sent)}:{sent[-1].get('at', '') if sent and isinstance(sent[-1], dict) else ''}"
    if why:
        return "", (f"<p class='why' data-key='{esc(key)}' data-sent='{esc(mark)}'>Review comments: "
                    f"{esc(why)}.</p>")
    href = f"/runs/ticket/{quote(key)}"
    return ("<p class='muted' id='dv-hint'>Click a line to comment on it; <a href='#dv-review'>the comments</a> go "
            f"to the fixer from the bottom of the page.{hint_more}</p>",
            f"<section class='panel' id='dv-review' data-key='{esc(key)}' data-sent='{esc(mark)}' "
            f"data-repo='{esc(repo)}' data-view='{esc(view)}' data-max='{runs_view.FIX_MAX}' "
            f"data-text-max='{runs_view.FIX_TEXT_MAX}' "
            "data-nomem><h2>Review comments</h2>"
            "<div class='muted'>Click a line of the diff to comment on it, or comment on the whole change here. "
            "The comments wait in this browser until you send them, those on the ticket's other repositories "
            "and views with them; then they all go straight to the fix stage, "
            "which does them and commits (a new plan is a follow-up, on the ticket's page).</div>"
            "<ol id='dv-list' class='dv-list'></ol>"
            f"<textarea id='dv-gen' maxlength='{runs_view.FIX_TEXT_MAX}' rows='3' "
            "aria-label='A comment on the whole change' placeholder='A comment on the whole change'></textarea>"
            "<div class='dv-row'><button type='button' class='btn sm' id='dv-add'>Add comment</button></div>"
            f"<form method='post' action='{href}/fix' id='dv-send'><input type='hidden' name='comments' value=''>"
            "<button type='submit' class='primary' disabled>Send 0 comments to the fixer</button></form></section>")


def page(runs, key, query):
    """(title, parts, script) for /runs/ticket/KEY/diff, or None when there is no such ticket."""
    st = _state(runs, key)
    if st is None:
        return None
    st.setdefault("key", key)
    choices = repos(runs, st)
    repo = (query.get("repo") or ["."])[0]
    bad = "" if repo in dict(choices) else f"not a repository of this ticket: {repo[:200]}"
    repo = repo if not bad else "."
    can = [v for v in VIEWS if v != "work" or has_work_view(st, repo)]
    view = (query.get("view") or [""])[0]
    if view not in can:
        view = "work" if "work" in can and st.get("base_sha") else "uncommitted"
    res = changes(runs, st, repo, view)
    left = left_uncommitted(st, repo)

    def says(v):
        return VIEW_SAYS["left" if v == "work" and left else v]
    href = f"/runs/ticket/{quote(key)}"
    opts = "".join(f"<option value='{esc(v)}'{' selected' if v == repo else ''}>{esc(label)}</option>"
                   for v, label in choices)
    radios = []
    for v, label in VIEWS.items():
        off = v not in can
        radios.append(f"<label class='{'off' if off else ''}' title='"
                      + esc("the ticket's change has no part in this repository" if off else says(v))
                      + f"'><input type='radio' name='view' value='{v}'{' checked' if v == view else ''}"
                      f"{' disabled' if off else ''}>{esc(label)}</label>")
    only = ("" if st.get("in_place") is True else
            "<div class='muted'>This ticket works in a separate copy, which holds only the project's own repository; "
            "the repositories inside your project folder are not in it.</div>")
    parts = [f"<style>{_asset('diff2html.min.css')}{STYLE}</style>",
             "<header class='ph'><div class='crumbs'><a href='/runs/queue'>Queue</a><span>/</span>"
             f"<a href='{href}'>{esc(key)}</a><span>/</span><b>Changes</b></div><h1>{esc(key)}: changes</h1></header>",
             "<section class='panel'>"
             f"<form id='dv-pick' class='dv-pick' method='get' action='{href}/diff'>"
             f"<label class='dv-repo'>Repository<select name='repo'>{opts}</select></label>"
             f"<div class='dv-pick dv-views' role='radiogroup' aria-label='What to show'>{''.join(radios)}</div>"
             "<noscript><button type='submit'>Show</button></noscript></form>"
             f"<div class='muted'>{esc(says(view))}" + (f": <code>{esc(res['says'])}</code>" if res["says"] else "")
             + f"</div>{only}</section>"]
    if bad:
        parts.append(runs_view._flash(bad + "; showing the project's own", True))
    take, why_not = uncommitted_takes_comments(runs, st, repo) if view == "uncommitted" else (False, "")
    if why_not and "work" in can and runs_view._tq() is not None:
        # the folder is on another branch than the ticket's: these files are not the ones fix works on
        work = f"<a href='{href}/diff?repo={quote(repo)}&amp;view=work'>The ticket's work</a>"
        parts.append(f"<p class='muted' id='dv-elsewhere'>{esc(why_not[0].upper() + why_not[1:])}, so these "
                     "uncommitted files are not the ones fix works on: review comments go on "
                     f"{work}.</p>")
    if res["error"]:
        parts.append(f"<section class='panel'><b>Nothing to show:</b> {esc(res['error'])}.</section>")
        return f"{key} changes", parts, JS
    if res["commits"]:
        parts.append(f"<section class='panel'><b>{len(res['commits'])}{'+' if len(res['commits']) >= LOG_CAP else ''}"
                     " commit(s)</b><ul class='dv-commits'>"
                     + "".join(f"<li><code>{esc(h)}</code> {esc(s)}</li>" for h, s in res["commits"]) + "</ul></section>")
    for n in res["notes"]:
        parts.append(f"<p class='why'>{esc(n)}.</p>")
    if not res["diff"].strip():
        parts.append("<section class='panel'>No changes.</section>")
        return f"{key} changes", parts, JS
    data = json.dumps(res["diff"]).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    if view == "work":
        hint, review = _review(runs, key, st, repo)
    elif take:   # an old line here is numbered as at HEAD, and the fixer is told so (owner_item's "at")
        hint, review = _review(runs, key, st, repo, view, (
            " This view compares the files with HEAD, so it also shows what you had changed here before the "
            "ticket started, and an old line is numbered as committed at HEAD; the fixer is told so."
            if left else " These are the ticket's branch's files not committed yet; what fix changes in them it "
            "commits on the branch, and an old line is numbered as committed at HEAD; the fixer is told so."))
    else:
        hint, review = "", ""
    parts += [hint, "<div class='dv-lay'><button type='button' class='btn sm' data-lay='side-by-side'>Side by side</button>"
              "<button type='button' class='btn sm' data-lay='line-by-line'>One column</button></div>",
              f"<script type='application/json' id='dv-data'>{data}</script><div id='d2h'></div>", review]
    return f"{key} changes", parts, ";" + _asset("diff2html.min.js") + ";" + JS
