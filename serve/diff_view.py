"""harness serve's Changes page for a queue ticket: a diff, drawn in the browser by
the vendored diff2html (vendor/README.md), of the repository picked from a list:
the project's own, or for a ticket that works in the project folder any repository
inside it (its own .git, not a submodule; the queue's nested_repos).

Three views: the ticket's work (its branch since the commit it started from, what
review reads), the uncommitted changes, and the commits not pushed yet. Read-only
git: no fetch, no optional locks, no external diff or textconv programs. Files that
look like secrets are left out of every diff (vault_view.SECRET)."""

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
    ticket works in the project folder (a copy holds only the project's own)."""
    out = [(".", "the project itself (" + (os.path.basename(st["project"].rstrip("/\\")) or st["project"]) + ")")]
    tq = runs_view._tq()
    if st.get("in_place") is True and tq and os.path.isdir(st["project"]):
        try:
            out += [(r, r) for r in tq.nested_repos(st["project"], skip=(runs.queue_dir,))]
        except Exception:   # an odd folder: the project's own still shows
            pass
    return out


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


def changes(runs, st, repo, view):
    """{"diff", "says", "notes", "commits", "error"} for one repository and view."""
    key, project = st["key"] if isinstance(st.get("key"), str) else "", st["project"]
    clone = os.path.join(runs.queue_dir, key, "repo")
    if repo == ".":
        d = project if st.get("in_place") is True else clone if os.path.isdir(clone) else None
    else:
        d = os.path.join(project, repo)
    res = {"diff": "", "says": "", "notes": [], "commits": [], "error": ""}
    if view == "work":
        branch = st.get("branch") or key
        base = st.get("base_sha") if isinstance(st.get("base_sha"), str) else ""
        if not base:
            res["error"] = "the ticket has not started yet, so it has no work to show"
            return res
        if d and _git(d, "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", cap=200)[0]:
            where, tip = d, f"refs/heads/{branch}"
        elif _git(project, "rev-parse", "-q", "--verify", f"refs/harness-queue/{key}^{{commit}}", cap=200)[0]:
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
/* the page's own table rules (right-aligned, padded cells) undone for diff2html's */
#d2h td,#d2h th{text-align:left;padding:0;border-bottom:0}
#d2h td.d2h-code-linenumber,#d2h td.d2h-code-side-linenumber{text-align:right}
#d2h td.d2h-code-side-linenumber{padding:0 .5em}
#d2h .d2h-file-list-wrapper{margin-bottom:12px}
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
    can = [v for v in VIEWS if v != "work" or repo == "."]
    view = (query.get("view") or [""])[0]
    if view not in can:
        view = "work" if repo == "." and st.get("base_sha") else "uncommitted"
    res = changes(runs, st, repo, view)
    href = f"/runs/ticket/{quote(key)}"
    opts = "".join(f"<option value='{esc(v)}'{' selected' if v == repo else ''}>{esc(label)}</option>"
                   for v, label in choices)
    radios = []
    for v, label in VIEWS.items():
        off = v not in can
        radios.append(f"<label class='{'off' if off else ''}' title='"
                      + esc("only the project's own repository has the ticket's branch" if off else VIEW_SAYS[v])
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
             f"<div class='muted'>{esc(VIEW_SAYS[view])}" + (f": <code>{esc(res['says'])}</code>" if res["says"] else "")
             + f"</div>{only}</section>"]
    if bad:
        parts.append(runs_view._flash(bad + "; showing the project's own", True))
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
    parts += ["<div class='dv-lay'><button type='button' class='btn sm' data-lay='side-by-side'>Side by side</button>"
              "<button type='button' class='btn sm' data-lay='line-by-line'>One column</button></div>",
              f"<script type='application/json' id='dv-data'>{data}</script><div id='d2h'></div>"]
    return f"{key} changes", parts, ";" + _asset("diff2html.min.js") + ";" + JS
