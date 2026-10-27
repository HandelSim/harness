"""harness serve's Settings page: harness's .env settings, the network allowlist,
MCP services, access codes and system checks.

Every change goes through the harness command a terminal user would type
(`harness config set`, `harness net allow`, `harness mcp enable`, ...), run with no
terminal so nothing can wait on a prompt; this page only shows and forwards."""

import os
import re
import shutil
import subprocess
import threading

import runs as runs_view

esc = runs_view.esc
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
MCP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
USERINFO = re.compile(r"://[^/@\s]+@")
MAX_VALUE = 8000
GROUPS = (("Upstream model API", ("PROXY_API_URL", "PROXY_API_KEY", "DEFAULT_MODEL_NAME", "MODEL_CONTEXT_LENGTH",
                                  "PROXY_TIMEOUT")),
          ("OpenAI-format API", ("OAI_API_URL", "OAI_API_KEY", "OAI_MODELS")),
          ("ChatGPT backend (harness chatgpt)", ("CHATGPT_BASE_URL", "CHATGPT_MODEL_NAME", "CHATGPT_COOKIE_STRING")),
          ("Ticket tracker", ("TRACKER_TOKEN",)),
          ("Corporate proxy", ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")))
MCP_ACTIONS = {"install": "installed", "enable": "enabled", "disable": "disabled"}
MARKS = {"✓": ("ok", "ok"), "⚠": ("wait", "warning"), "✗": ("bad", "problem"),
         "–": ("off", "skipped")}

STYLE = """
.set-nav { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }
.set-nav a { padding: 5px 11px; border: 1px solid var(--line); border-radius: 999px; color: var(--ink-2); font-size: 13px; }
.set-nav a:hover { text-decoration: none; border-color: var(--line-2); color: var(--ink); }
.cfg { display: flex; flex-direction: column; gap: 4px; }
.cfg h3 { margin: 14px 0 4px; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.cfg h3:first-child { margin-top: 0; }
.cfg .row { display: grid; grid-template-columns: minmax(0, 260px) minmax(0, 1fr); gap: 4px 16px; padding: 8px 0;
  border-top: 1px solid var(--line); align-items: start; }
@media (max-width: 720px) { .cfg .row { grid-template-columns: minmax(0, 1fr); } }
.cfg .k { font: 600 12.5px/1.4 var(--mono); overflow-wrap: anywhere; }
.cfg .d { display: block; font: 12.5px/1.45 var(--sans); color: var(--muted); margin-top: 2px; }
.cfg input[type=text], .cfg input[type=password] { width: 100%; box-sizing: border-box; }
.cfg .clr { display: inline-flex; gap: 6px; align-items: center; font-size: 12.5px; color: var(--muted); margin-top: 4px; }
.cfg .save { display: flex; gap: 12px; align-items: center; justify-content: flex-end; padding-top: 10px; flex-wrap: wrap; }
.slist { list-style: none; margin: 0; padding: 0; }
.slist li { display: flex; gap: 6px 12px; align-items: center; justify-content: space-between; padding: 7px 0;
  border-top: 1px solid var(--line); flex-wrap: wrap; }
.slist li:first-child { border-top: 0; }
.slist .nm { font-family: var(--mono); font-size: 13px; overflow-wrap: anywhere; min-width: 0; }
.slist .acts { display: flex; gap: 6px; flex-wrap: wrap; }
.inl { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 10px; }
.inl input[type=text] { flex: 1; min-width: 160px; }
.doc { margin: 0; padding: 0; list-style: none; font-size: 13px; }
.doc li { display: grid; grid-template-columns: 18px minmax(0, 190px) minmax(0, 1fr); gap: 8px; padding: 3px 0; }
.doc li.sec { display: block; font-weight: 650; margin-top: 10px; color: var(--muted); }
.doc li .n { font-family: var(--mono); overflow-wrap: anywhere; } .doc li .t { overflow-wrap: anywhere; color: var(--ink-2); }
.doc .ok { color: var(--ok); } .doc .wait { color: var(--wait); } .doc .bad { color: var(--bad); } .doc .off { color: var(--faint); }
.newcode { font: 600 15px/1.5 var(--mono); padding: 10px 12px; background: var(--sunk); border: 1px dashed var(--line-2);
  border-radius: 8px; overflow-wrap: anywhere; user-select: all; }
pre.out { white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.5 var(--mono); max-height: 420px; overflow: auto;
  background: var(--sunk); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin: 8px 0 0; }
"""


def run_harness(harness, install_root, *args, timeout=60):
    """(exit status, output) of `harness ARGS` with no terminal (it cannot prompt), or
    (None, why) when it could not run or took too long."""
    bash = shutil.which("bash")
    if not bash:
        return None, "bash is not on PATH, so harness cannot run"
    if not harness or not os.path.isfile(harness):
        return None, f"no harness script at {harness or '(none)'} (pub.py --harness)"
    env = dict(os.environ, HARNESS_OPEN="0", NO_COLOR="1", TERM="dumb")
    if install_root and not env.get("HARNESS_INSTALL_ROOT"):
        env["HARNESS_INSTALL_ROOT"] = install_root
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kw["start_new_session"] = True        # no controlling terminal: a /dev/tty prompt fails at once
    home = os.path.expanduser("~")
    try:
        r = subprocess.run([bash, harness, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, env=env,
                           cwd=home if os.path.isdir(home) else None, **kw)
    except subprocess.TimeoutExpired:
        return None, f"harness {' '.join(args)} took over {timeout} s and was stopped"
    except OSError as e:
        return None, f"cannot run harness: {e.strerror or e}"
    return r.returncode, ANSI.sub("", r.stdout.decode("utf-8", "replace")).replace("\r\n", "\n")


def _last_line(text):
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return lines[-1] if lines else ""


# --- reading ---------------------------------------------------------------------

def read_config(h):
    rc, out = run_harness(*h, "config", "list", "--tsv", timeout=20)
    if rc != 0:
        return None, out
    rows = []
    for line in out.splitlines():
        bits = line.split("\t", 3)
        if len(bits) == 4 and KEY_RE.match(bits[0]):
            # a proxy URL with a password in it is treated as a secret here too
            secret = bits[2] == "1" or bool(USERINFO.search(bits[1]))
            rows.append({"key": bits[0], "value": USERINFO.sub("://***@", bits[1]) if secret and bits[2] != "1"
                         else bits[1], "secret": secret, "desc": bits[3]})
    return rows, ""


def read_allowlist(h):
    rc, out = run_harness(*h, "net", "list", timeout=20)
    if rc != 0:
        return None, _last_line(out)
    hosts = []
    for line in out.splitlines():
        m = re.match(r"^  (\S+)\s*(\[git-push\])?\s*(#.*)?$", line)
        if m and HOST_RE.match(m.group(1)):
            hosts.append({"host": m.group(1), "push": bool(m.group(2)), "note": (m.group(3) or "")[1:].strip()})
    return hosts, ""


def read_mcp(h):
    rc, out = run_harness(*h, "mcp", "list", "--available", timeout=40)
    if rc != 0:
        return None, _last_line(out)
    rows = []
    for line in out.splitlines()[1:]:
        bits = line.split(None, 2)
        if len(bits) >= 2 and MCP_RE.match(bits[0]):
            rows.append({"name": bits[0], "state": bits[1], "status": bits[2].strip() if len(bits) > 2 else ""})
    return rows, ""


def read_doctor(h):
    rc, out = run_harness(*h, "doctor", timeout=120)
    return rc, out


def gather(h):
    """The page's slow parts, read side by side."""
    got = {}
    jobs = {"config": read_config, "net": read_allowlist, "mcp": read_mcp}

    def one(name, fn):
        try:
            got[name] = fn(h)
        except Exception as e:      # noqa: BLE001  one failed part must not take the page down
            got[name] = (None, f"could not read: {e}")
    threads = [threading.Thread(target=one, args=(n, f), daemon=True) for n, f in jobs.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(130)
    return {n: got.get(n, (None, "took too long")) for n in jobs}


# --- changing ------------------------------------------------------------------------

def save_config(h, form):
    """Set the keys whose value changed: a typed secret replaces it, Clear empties it;
    a secret left blank stays as it is. (ok, message)."""
    rows, err = read_config(h)
    if rows is None:
        return False, err or "cannot read the settings"
    changed, failed = [], []
    for row in rows:
        key = row["key"]
        if row["secret"]:
            new = (form.get(f"v_{key}") or [""])[0]
            if (form.get(f"clear_{key}") or [""])[0] == "1":
                new = ""
            elif not new:
                continue
        else:
            if f"v_{key}" not in form:
                continue
            new = (form.get(f"v_{key}") or [""])[0].strip()
            if new == row["value"]:
                continue
        if "\n" in new or "\r" in new or "\0" in new or len(new) > MAX_VALUE:
            failed.append(f"{key}: one line, at most {MAX_VALUE} characters")
            continue
        rc, out = run_harness(*h, "config", "set", key, new, timeout=20)
        (changed if rc == 0 else failed).append(key if rc == 0 else f"{key}: {_last_line(out)}")
    if failed:
        return False, "Not saved: " + "; ".join(failed) + (f" (saved: {', '.join(changed)})" if changed else "")
    if not changed:
        return True, "Nothing changed."
    return True, (f"Saved {', '.join(changed)}. Commands started from now on use the new value; anything already "
                  "running (the proxy, a queue stage) keeps what it started with until it restarts.")


def net_change(h, form):
    act = (form.get("act") or [""])[0]
    host = (form.get("host") or [""])[0].strip().lower()
    if not HOST_RE.match(host or "-") or act not in ("allow", "deny"):
        return False, "give a host name: lowercase letters, digits, dots and dashes"
    args = ["net", act, host] + (["--git-push"] if act == "allow" and (form.get("push") or [""])[0] == "1" else [])
    rc, out = run_harness(*h, *args, timeout=20)
    lines = [x for x in out.splitlines() if x.strip() and not x.startswith("next:")]
    msg = lines[-1].strip() if lines else ""
    if rc != 0:
        return False, msg or f"harness net {act} failed"
    return True, f"{msg[:1].upper()}{msg[1:]}. Running containers pick this up when they restart (harness restart)."


def mcp_change(h, form):
    act, name = (form.get("act") or [""])[0], (form.get("name") or [""])[0]
    if act not in MCP_ACTIONS or not MCP_RE.match(name):
        return False, "unknown MCP action"
    rc, out = run_harness(*h, "mcp", act, name, timeout=120)
    if rc != 0:
        return False, _last_line(out) or f"harness mcp {act} {name} failed"
    return True, f"{name} {MCP_ACTIONS[act]}. " + ("It starts with the stack next time (harness restart)."
                                                  if act != "disable" else "It no longer starts with the stack.")


def system_info(h):
    harness, root = h
    out = {"root": root or "?"}
    clone = os.path.dirname(os.path.abspath(harness)) if harness else ""
    try:
        r = subprocess.run(["git", "-C", clone, "log", "-1", "--format=%h, %cr: %<(70,trunc)%s"], capture_output=True,
                           timeout=5, stdin=subprocess.DEVNULL, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
        b = subprocess.run(["git", "-C", clone, "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True,
                           timeout=5, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            branch = b.stdout.decode(errors="replace").strip()
            out["version"] = (f"{branch} at " if branch else "") + r.stdout.decode(errors="replace").strip()[:160]
    except (OSError, subprocess.SubprocessError):
        pass
    out["code"] = clone
    return out


def system_action(h, act):
    """(ok, message, (what, status, output) to show) for the System buttons."""
    if act == "doctor":
        rc, out = run_harness(*h, "doctor", timeout=120)
        return rc is not None, "harness doctor ran." if rc == 0 else "harness doctor found problems (marked \u2717).", \
            ("doctor", rc, out)
    if act == "check":
        rc, out = run_harness(*h, "check-updates", timeout=40)
        return rc == 0, _last_line(out) or "checked", ("check-updates", rc, out)
    if act == "update":
        rc, out = run_harness(*h, "update", timeout=180)
        if rc == 0:
            return True, ("harness is updated. Restart harness serve to load new pages; containers pick up new "
                          "images on the next harness upgrade."), ("update", rc, out)
        return False, "harness update did not finish: " + (_last_line(out) or "see the output"), ("update", rc, out)
    return False, "unknown action", None


# --- rendering ---------------------------------------------------------------------

def _config_panel(rows, err):
    if rows is None:
        return f"<p class='err'>{esc(err or 'cannot read the settings')}</p>"
    by = {r["key"]: r for r in rows}
    seen, parts = set(), ["<form method='post' action='/settings/config' class='cfg' autocomplete='off'>"]
    groups = list(GROUPS) + [("Other", tuple(k for k in by if not any(k in g for _, g in GROUPS)))]
    for title, keys in groups:
        keys = [k for k in keys if k in by]
        if not keys:
            continue
        parts.append(f"<h3>{esc(title)}</h3>")
        for k in keys:
            seen.add(k)
            r = by[k]
            if r["secret"]:
                ph = f"{r['value']}: type to replace" if r["value"] else "not set"
                field = (f"<input type='password' name='v_{esc(k)}' id='v_{esc(k)}' autocomplete='new-password' "
                         f"placeholder='{esc(ph)}'>")
                if r["value"]:
                    field += (f"<label class='clr'><input type='checkbox' name='clear_{esc(k)}' value='1'>"
                              "clear it</label>")
            else:
                field = (f"<input type='text' name='v_{esc(k)}' id='v_{esc(k)}' value='{esc(r['value'])}' "
                         f"placeholder='not set' spellcheck='false'>")
            parts.append(f"<div class='row'><label for='v_{esc(k)}'><span class='k'>{esc(k)}</span>"
                         f"<span class='d'>{esc(r['desc'])}</span></label><div>{field}</div></div>")
    parts.append("<div class='save'><span class='muted'>Saved to harness's .env with <code>harness config set</code>. "
                 "Secrets are never shown; leave one blank to keep it.</span>"
                 "<button type='submit' class='primary'>Save changes</button></div></form>")
    return "".join(parts)


def _net_panel(hosts, err):
    if hosts is None:
        return f"<p class='err'>{esc(err)}</p>"
    rows = "".join(
        f"<li><span class='nm'>{esc(x['host'])}" + (" <span class='pill t-acc'>git push</span>" if x["push"] else "")
        + (f" <span class='muted'>{esc(x['note'])}</span>" if x["note"] else "") + "</span>"
        + runs_view._btn("/settings/net", "Remove", "ghost", (("act", "deny"), ("host", x["host"])),
                         confirm=f"Remove {x['host']} from the allowlist? Agents in containers can no longer reach it.")
        + "</li>" for x in hosts)
    return (f"<p class='muted'>Containers reach only these hosts ({len(hosts)}). Pull only, unless a host allows "
            "git push. Host-mode agents are not limited by this list.</p>"
            f"<ul class='slist'>{rows}</ul>"
            "<form method='post' action='/settings/net' class='inl'><input type='hidden' name='act' value='allow'>"
            "<input type='text' name='host' placeholder='host to allow, e.g. gitlab.example.com' required "
            "aria-label='host to allow' spellcheck='false' autocapitalize='off'>"
            "<label class='clr'><input type='checkbox' name='push' value='1'>allow git push</label>"
            "<button type='submit'>Allow</button></form>")


def _mcp_panel(rows, err):
    if rows is None:
        return f"<p class='err'>{esc(err)}</p>"
    if not rows:
        return "<p class='muted'>No MCP services in the registry.</p>"
    items = []
    for r in rows:
        st = r["state"]
        if st == "available":
            acts, pill = (("install", "Install"),), "<span class='pill t-off'>not installed</span>"
        elif st == "installed-enabled":
            acts, pill = (("disable", "Disable"),), "<span class='pill t-ok'>enabled</span>"
        else:
            acts, pill = (("enable", "Enable"),), "<span class='pill t-wait'>disabled</span>"
        btns = "".join(runs_view._btn("/settings/mcp", label, "ghost", (("act", a), ("name", r["name"])))
                       for a, label in acts)
        status = f" <span class='muted'>{esc(r['status'])}</span>" if r["status"] not in ("", "-") else ""
        items.append(f"<li><span class='nm'>{esc(r['name'])} {pill}{status}</span><span class='acts'>{btns}</span></li>")
    return ("<p class='muted'>Tool servers the agents can use. Enabled ones start with the container stack.</p>"
            f"<ul class='slist'>{''.join(items)}</ul>")


def _access_panel(codes, user, new_code=None):
    if codes is None:
        return ("<p class='muted'>Access codes are off, so this page answers only on this computer. To let "
                "someone in from elsewhere, make them a code in a terminal with "
                "<code>harness serve code add NAME</code> and restart harness serve; codes are then managed "
                "here.</p>")
    names = sorted({n for n, _ in codes.entries()})
    rows = "".join(
        f"<li><span class='nm'>{esc(n)}" + (" <span class='pill t-acc'>you</span>" if n == user else "") + "</span>"
        + ("" if n == user else runs_view._btn("/settings/access", "Remove", "ghost", (("act", "remove"), ("name", n)),
                                               confirm=f"Remove {n}'s code? They are signed out at once."))
        + "</li>" for n in names)
    shown = ""
    if new_code:
        shown = (f"<p>The code for <b>{esc(new_code[0])}</b>, shown once (it is stored only as a hash). "
                 f"Send it to them privately:</p><div class='newcode'>{esc(new_code[1])}</div>")
    return (shown + f"<p class='muted'>Who can sign in ({len(names)}). Everyone signed in can do everything here.</p>"
            f"<ul class='slist'>{rows}</ul>"
            "<form method='post' action='/settings/access' class='inl'><input type='hidden' name='act' value='add'>"
            "<input type='text' name='name' placeholder='name (letters, digits, . _ -)' required maxlength='32' "
            "pattern='[A-Za-z0-9_.\\-]{1,32}' aria-label='name for the new code'>"
            "<button type='submit'>Make a code</button></form>")


def doctor_list(out):
    """harness doctor's report as a list: sections, then one line a check with its mark."""
    items = []
    for line in (out or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^\[(.+)\]$", s)
        if m:
            items.append(f"<li class='sec'>{esc(m.group(1))}</li>")
            continue
        mark = s[0]
        if mark in MARKS:
            cls, label = MARKS[mark]
            name, _, text = s[1:].strip().partition("  ")
            items.append(f"<li><span class='{cls}' title='{label}'>{esc(mark)}</span><span class='n'>{esc(name)}</span>"
                         f"<span class='t'>{esc(text.strip())}</span></li>")
        else:
            items.append(f"<li class='sec'>{esc(s)}</li>")
    return f"<ul class='doc'>{''.join(items)}</ul>"


def _system_panel(info, result=None):
    parts = [f"<dl class='facts'><dt>harness</dt><dd>{esc(info.get('version') or '?')}</dd>"
             f"<dt>installed at</dt><dd>{esc(info.get('root') or '?')}</dd>"
             f"<dt>code</dt><dd>{esc(info.get('code') or '?')}</dd></dl>",
             "<div class='inl'>",
             runs_view._btn("/settings/system", "Run doctor", "", (("act", "doctor"),)),
             runs_view._btn("/settings/system", "Check for updates", "ghost", (("act", "check"),)),
             runs_view._btn("/settings/system", "Update harness", "ghost", (("act", "update"),),
                            confirm="Fast-forward harness to the latest version? Running agents keep going; "
                                    "restart harness serve afterwards to load the new pages."),
             "</div>"]
    if result:
        what, rc, out = result
        if what == "doctor" and rc is not None:
            parts.append(f"<h3>harness doctor</h3>{doctor_list(out)}")
        else:
            parts.append(f"<h3>harness {esc(what)}</h3><pre class='out'>{esc(out.strip() or '(no output)')}</pre>")
    return "".join(parts)


def render(h, codes, user, info, flash="", bad=False, new_code=None, result=None):
    g = gather(h)
    sections = (("model", "Model and keys", _config_panel(*g["config"])),
                ("network", "Network allowlist", _net_panel(*g["net"])),
                ("mcp", "MCP services", _mcp_panel(*g["mcp"])),
                ("access", "Access", _access_panel(codes, user, new_code)),
                ("system", "System", _system_panel(info, result)))
    chips = "".join(f"<a href='#{k}'>{esc(t)}</a>" for k, t, _ in sections)
    body = "".join(f"<section class='panel' id='{k}'><div class='hd'><h2>{esc(t)}</h2></div>{html}</section>"
                   for k, t, html in sections)
    return [f"<style>{STYLE}</style>",
            "<div class='ph'><h1>Settings</h1><div class='sub'>How harness reaches its model, what the agents "
            "may reach, the tools they get, who can sign in here, and harness itself.</div></div>",
            runs_view._flash(flash, bad), f"<nav class='set-nav'>{chips}</nav>",
            f"<div class='stack' style='max-width:980px'>{body}</div>"]
