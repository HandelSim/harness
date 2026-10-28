"""The pub page's run viewer (/runs): every opencode session harness agents ran, read
from opencode's own databases, so it covers container agents, host mode, opencode
web and `harness queue` stages alike, finished or still working; and the queue's
control room: its runner, its tickets, and every action harness queue has.

  /runs                       live runs, queue tickets by stage, then everything else
  /runs/queue                 the queue: the runner (start, stop), every ticket in
                              order with its stage strip and row actions, and a form
                              to add tickets (stages, start, branch, how the changes land in each
                              repository, plan approval, a note)
  /runs/queue/live            the queue page's live regions, as JSON (polled)
  /runs/queue/repos?project=P the Add form's tree, as JSON: the project's repository and
                              those nested in it, each with its branch, uncommitted
                              files, ahead/behind its upstream and its branches
  /runs/<session>             the transcript: messages, thinking, every tool call with
                              its input and output, subagent sessions nested; follows a
                              working session as it goes, and takes a message for a
                              headless run that is still live
  /runs/follow?since=MS[&queue=1]
                              what a launch opens: the newest session started since
                              MS (with queue=1, queue stages only, and moving on to
                              each next stage); waits for one to start
  /runs/follow.json?...       {"sid": that session, or null}
  /runs/<session>.json        the session, its messages and parts, as stored
  /runs/<session>/tail?from=N messages N.. as HTML, for the page's polling
  /runs/<session>/part/<id>   one tool call's full output, as text
  /runs/ticket/<KEY>          a queue ticket: status, stage strip, every action its
                              status allows, owner notes, the plan's questions (with
                              a form to answer them), every session run for it,
                              each attempt and why it failed, plan/review/answers,
                              log.md and stage outputs
  /runs/ticket/<KEY>/live     the ticket page's live regions, as JSON (polled)
  POST /runs/<session>/send   a message for a live run (agents/live-run.sh's inbox)
  POST /runs/<session>/stop   stop the queue stage the session belongs to
  POST /runs/ticket/<KEY>/answer|approve|retry|stop|remove|move|note
                              harness queue's own commands for the ticket
  POST /runs/queue/add|start|halt
                              add tickets; start `harness queue run` detached (back=KEY:
                              back to that ticket's page); halt it

Each source is a data directory holding opencode/opencode.db and harness/live/ (the
markers and inboxes agents/live-run.sh keeps). The databases are opened read-only.
"""

import calendar
import hashlib
import html
import json
import os
import posixpath
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import quote

SID_RE = re.compile(r"^ses_[A-Za-z0-9]{1,64}$")
PART_RE = re.compile(r"^prt_[A-Za-z0-9]{1,64}$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
OUT_RE = re.compile(r"^[a-z]+-[0-9]+\.out$")
# A git ref or commit as the add form takes it: no leading '-' (an option), no
# spaces, no '..' (a range), nothing git would read as revision syntax.
REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")
LIVE_FRESH = 10          # seconds since the relay last touched the marker
ACTIVE = 90              # an interactive session updated this recently is shown as active
RUN_FRESH = 30           # seconds since the queue runner's heartbeat (run.json) for it to count as running
LIST_CAP = 300
TEXT_CAP = 20000         # characters of one tool input/output shown inline
MAX_MESSAGE = 65536      # bytes of one message sent to a live run
NOTE_MAX = 4000         # characters of one owner note
ADD_NOTE_MAX = 4000      # the note given with add (it goes on a command line)
# Windows: a child with a console of its own that has no window. Without it each git
# or queue call a click makes flashes a console window when serve has no console
# (the run viewer a launch leaves up) or a hidden one.
NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
MAX_KEYS = 50
FORM_QUESTIONS = 6       # questions with their own answer box (the form has at most 20 fields)
REPO_ROWS = 50           # repositories the Add form's tree lists, the project's own first
REPO_REFS = 200          # branch names of one repository offered as its target
REPO_BUDGET = 30         # s all of the tree's git calls may take
REPO_MODES = ("uncommitted", "branch")   # how a ticket's changes land in a repository
ADD_FIELDS = 20 + 3 * REPO_ROWS          # the Add form: its own fields, then a mode, branch and target per repository
HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE_SCRIPT = os.path.join(HERE, "..", "scripts", "ticket_queue.py")
HARNESS = os.path.join(HERE, "..", "harness")
WAITING = ("needs-answers", "plan-review", "blocked")   # a ticket waiting on the owner
STAGES = ("plan", "implement", "review", "fix", "push")
PRESETS = {"full": STAGES, "review": ("review",), "implement": ("implement", "review", "fix", "push"),
           "plan": ("plan",)}
PRESET_LABELS = (("full", "Full", "plan, implement, review, fix, push"),
                 ("review", "Review only", "review existing work; nothing is pushed"),
                 ("implement", "Implement + review", "no plan: implement, review, fix, push"),
                 ("plan", "Plan only", "write the plan and stop"))
MOVES = ("up", "down", "top", "bottom")

esc = html.escape
_TQ = []   # scripts/ticket_queue.py as a module, loaded on first use; [None] when it cannot be


def _tq():
    """The queue's own module, for the rules it applies (where a retry may go), or None."""
    if not _TQ:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(QUEUE_SCRIPT)))
            import ticket_queue
            _TQ.append(ticket_queue)
        except Exception:   # an older or broken checkout: the page offers what it did before
            _TQ.append(None)
    return _TQ[0]

STYLE = """
/* The run viewer and queue: neutral greys, one indigo accent, status colours that mean one
   thing each (blue running, amber waiting on you, red blocked/failed, green done, grey
   stopped). System fonts only; works offline. */
.bar { position: sticky; top: 0; z-index: 20; background: var(--bg);
  background: color-mix(in srgb, var(--bg) 88%, transparent); backdrop-filter: saturate(1.4) blur(10px);
  -webkit-backdrop-filter: saturate(1.4) blur(10px); border-bottom: 1px solid var(--line); }
.bar-in { max-width: 1280px; margin: 0 auto; height: 52px; display: flex; align-items: center; gap: 14px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.logo { font: 700 15px/1 var(--mono); color: var(--ink); letter-spacing: -.02em; white-space: nowrap; }
.logo::before { content: "~$ "; color: var(--accent); }
.logo:hover { text-decoration: none; }
.tabs { display: flex; gap: 2px; min-width: 0; }
.tabs a { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 7px; color: var(--muted);
  font-weight: 550; font-size: 14px; white-space: nowrap; }
.tabs a:hover { color: var(--ink); background: var(--raised); text-decoration: none; }
.tabs a.on { color: var(--ink); background: var(--raised); box-shadow: inset 0 0 0 1px var(--line); }
.badge { display: inline-grid; place-items: center; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
  font-size: 11px; font-weight: 700; background: var(--wait); color: var(--wait-ink); }
.bar-mid { flex: 1; display: flex; align-items: center; gap: 14px; min-width: 0; }
.bar-right { margin-left: auto; display: flex; align-items: center; gap: 8px; min-width: 0; }
.runchip { display: inline-flex; align-items: center; gap: 8px; padding: 4px 10px; border-radius: 999px;
  border: 1px solid var(--line); font-size: 12.5px; color: var(--muted); white-space: nowrap; min-width: 0; }
.runchip b { color: var(--ink); font-weight: 600; overflow: hidden; text-overflow: ellipsis; }
.runchip:hover { text-decoration: none; border-color: var(--line-2); }
main.rv { max-width: 1280px; gap: 18px; padding-top: 22px; }
.fresh { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; min-width: 0; max-width: 46ch; }
.fresh::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--ok); flex: none; }
.fresh.warn { color: var(--wait); } .fresh.warn::before { background: var(--wait); }
.fresh.bad { color: var(--bad); } .fresh.bad::before { background: var(--bad); }
.fresh[hidden] { display: none; }
.bar-who { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); white-space: nowrap; }
.bar-who span { max-width: 14ch; overflow: hidden; text-overflow: ellipsis; }
.bar .bar-who button { padding: 4px 9px; font-size: 12.5px; }
.ph { display: flex; align-items: center; gap: 10px 14px; flex-wrap: wrap; min-width: 0; }
.ph h1 { font-size: 22px; font-weight: 680; letter-spacing: -.01em; overflow-wrap: anywhere; }
.ph .sub { flex-basis: 100%; color: var(--muted); font-size: 14px; overflow-wrap: anywhere; }
.crumbs { display: flex; gap: 6px; flex-wrap: wrap; align-items: baseline; font-size: 13px; color: var(--muted);
  flex-basis: 100%; min-width: 0; }
.crumbs a { color: var(--muted); } .crumbs a:hover { color: var(--ink); }
.crumbs span { color: var(--faint); }
.crumbs b { color: var(--ink); font-weight: 550; overflow-wrap: anywhere; }
.rv .panel h2, .rv h2 { font-size: 14px; font-weight: 650; letter-spacing: 0; text-transform: none; color: var(--ink);
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rv h2 .muted { font-weight: 450; }
.panel > .hd { display: flex; align-items: center; justify-content: space-between; gap: 8px 12px; flex-wrap: wrap; }
.panel.flush { padding: 0; gap: 0; overflow: hidden; }
.panel.flush > .hd { padding: 14px 18px; border-bottom: 1px solid var(--line); }

/* pills: one per state */
.pill { display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 9px; border-radius: 999px;
  font-size: 12px; font-weight: 600; line-height: 1; white-space: nowrap; border: 1px solid transparent;
  vertical-align: middle; }
.t-run { color: var(--run); background: var(--run-soft); }
.t-wait { color: var(--wait); background: var(--wait-soft); }
.t-bad { color: var(--bad); background: var(--bad-soft); }
.t-ok { color: var(--ok); background: var(--ok-soft); }
.t-off { color: var(--muted); background: var(--sunk); }
.t-queued { color: var(--ink-2); border-color: var(--line-2); }
.t-acc { color: var(--accent); background: var(--accent-soft); }
.dot::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex: none; }
.pulse::before { animation: pulse 1.6s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: .3; } }
@media (prefers-reduced-motion: reduce) { .pulse::before { animation: none; } }
.sdot { width: 9px; height: 9px; border-radius: 50%; background: var(--faint); flex: none; }
.sdot.run { background: var(--run); box-shadow: 0 0 0 4px var(--run-soft); }
.sdot.wait { background: var(--wait); }

/* the stage strip: plan implement review fix push */
.strip { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 4px; list-style: none; padding: 0;
  margin: 0; min-width: 0; }
.strip li { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.strip i { display: block; height: 5px; border-radius: 3px; background: var(--line); }
.strip span { font-size: 11px; color: var(--faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.strip .done i { background: var(--ok); } .strip .done span { color: var(--muted); }
.strip .skip i { background: var(--ok); opacity: .3; }
.strip .off i { background: transparent; border: 1px dashed var(--line-2); height: 5px; }
.strip .off span { text-decoration: line-through; }
.strip .cur span { color: var(--ink); font-weight: 650; }
.strip .cur.run i { background: var(--run); } .strip .cur.wait i { background: var(--wait); }
.strip .cur.bad i { background: var(--bad); } .strip .cur.queued i { background: var(--ink-2); opacity: .55; }
.strip .cur.stopped i { background: var(--muted); }
.strip.big { gap: 6px; } .strip.big i { height: 7px; } .strip.big span { font-size: 12.5px; }
.strip.big em { font-style: normal; font-size: 11.5px; color: var(--muted); }

/* the queue's tickets */
.tks { display: flex; flex-direction: column; }
.tk { display: grid; grid-template-columns: 30px minmax(0, 1fr) minmax(200px, 320px) 64px 320px;
  gap: 6px 18px; align-items: center; padding: 12px 18px; border-top: 1px solid var(--line); min-width: 0; }
.tk:first-child { border-top: 0; }
.tk:hover { background: var(--hover); }
.tk.wait { box-shadow: inset 3px 0 0 var(--wait); background: linear-gradient(90deg, var(--wait-soft), transparent 55%); }
.tk.run { box-shadow: inset 3px 0 0 var(--run); }
.tk .pos { color: var(--faint); font: 12px var(--mono); text-align: right; }
.tk .main { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.tk .key { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tk .key a { font: 650 14.5px var(--mono); color: var(--ink); }
.tk .why { font-size: 13px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tk.wait .why { color: var(--wait); }
.tk .when { font-size: 12px; color: var(--muted); white-space: nowrap; text-align: right; }
.ra { display: flex; gap: 4px; justify-content: flex-end; flex-wrap: wrap; }
.ra form { margin: 0; }
.empty { padding: 28px 18px; color: var(--muted); text-align: center; font-size: 14px; }

/* buttons and fields */
.btn, .panel button, .bar button, form button { display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  height: 32px; padding: 0 12px; border-radius: 7px; border: 1px solid var(--line-2); background: var(--raised);
  color: var(--ink); font: 550 13px/1 var(--sans); cursor: pointer; white-space: nowrap; text-decoration: none; }
.btn:hover, form button:hover { background: var(--hover); border-color: var(--line-3); text-decoration: none; }
form button.primary, .btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
form button.primary:hover, .btn.primary:hover { filter: brightness(1.1); background: var(--accent); }
form button.danger { color: var(--bad); } form button.danger:hover { background: var(--bad-soft); border-color: var(--bad); }
form button.warn { color: var(--wait); } form button.warn:hover { background: var(--wait-soft); border-color: var(--wait); }
form button.sm, .btn.sm { height: 26px; padding: 0 8px; font-size: 12px; border-radius: 6px; }
form button.ic { width: 26px; padding: 0; font-size: 12px; }
form button:disabled { opacity: .4; cursor: default; }
.panel input[type=text], .panel input[type=search], .panel input:not([type]), .panel select, .panel textarea,
main input[type=search] { width: 100%; height: 34px; padding: 0 10px; background: var(--field); border: 1px solid var(--line-2);
  border-radius: 7px; color: var(--ink); font: 14px var(--sans); }
.panel textarea { height: auto; min-height: 84px; padding: 8px 10px; line-height: 1.5; resize: vertical; }
.panel select { width: auto; padding-right: 6px; }
.panel input:focus, .panel textarea:focus, .panel select:focus, main input[type=search]:focus { outline: none;
  border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
::placeholder { color: var(--faint); }
.fld { display: flex; flex-direction: column; gap: 5px; min-width: 0; font-size: 13px; font-weight: 550; }
.fld small { font-weight: 400; color: var(--muted); font-size: 12px; }
.grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
fieldset { border: 0; padding: 0; margin: 0; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
legend { font-size: 13px; font-weight: 550; padding: 0; margin-bottom: 6px; }
.presets { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; }
.presets label { display: flex; flex-direction: column; gap: 2px; padding: 8px 10px; border: 1px solid var(--line-2);
  border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600; min-width: 0; }
.presets label small { font-weight: 400; color: var(--muted); font-size: 11.5px; line-height: 1.35; }
.presets input { position: absolute; opacity: 0; pointer-events: none; }
.presets label:has(input:checked) { border-color: var(--accent); background: var(--accent-soft); }
.presets label:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
.presets.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.presets.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.presets label input[name=cont] { position: static; opacity: 1; pointer-events: auto; margin-top: 6px; width: 100%; }
.projhint { font-size: 12px; min-height: 1em; margin-top: -4px; }
.projhint.err { color: var(--bad); }
.why { font-size: 12px; color: var(--wait); font-weight: 400; }
.presets label:has(input:disabled), .checks label:has(input:disabled) { opacity: .5; cursor: not-allowed; }
.presets label:has(input:disabled) .why { opacity: 1; }
fieldset:disabled .presets label { opacity: .5; cursor: not-allowed; }
#addhint { font-size: 13px; } #addhint div + div { margin-top: 2px; } #addhint .warn { color: var(--wait); }
details.adv > summary { cursor: pointer; font: 600 13px/1.6 var(--sans); padding: 2px 0; }
details.adv[open] > summary { margin-bottom: 4px; }
.checks { display: flex; gap: 6px; flex-wrap: wrap; }
.checks label { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
  border: 1px solid var(--line-2); font-size: 13px; cursor: pointer; }
.checks label:has(input:checked) { border-color: var(--accent); color: var(--ink); background: var(--accent-soft); }
.checks input, .opt input { accent-color: var(--accent); margin: 0; }
.opt { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; }
.opt small { display: block; color: var(--muted); font-size: 12px; }
.formacts { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
#addform [hidden] { display: none; }
/* the Add form's repositories: one row each, indented by depth (set inline) */
.repotree { display: flex; flex-direction: column; border: 1px solid var(--line-2); border-radius: 8px; }
.rrow { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; padding: 6px 10px;
  border-top: 1px solid var(--line); font-size: 12.5px; color: var(--muted); }
.rrow:first-child { border-top: 0; }
.rrow .rpath { font: 600 12.5px var(--mono); color: var(--ink); overflow-wrap: anywhere; }
.rrow .hot { color: var(--wait); font-weight: 600; }
.rrow .err { color: var(--bad); }
.rrow select { height: 28px; font-size: 12.5px; margin-left: auto; }
.rrow .rbr { display: flex; flex-wrap: wrap; gap: 6px 12px; flex-basis: 100%; }
.rrow .rbr label { display: flex; align-items: center; gap: 6px; font-weight: 550; color: var(--ink); }
.rrow .rbr input { height: 28px; width: 22ch; font-size: 12.5px; }

/* the runner card */
.runner { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px 24px; align-items: center; }
.runner .state { display: flex; align-items: center; gap: 12px; min-width: 0; }
.runner .state h2 { font-size: 16px; }
.runner .facts { grid-column: 1 / -1; }
.runner .startf { display: flex; gap: 8px 16px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.kv { display: flex; gap: 6px 22px; flex-wrap: wrap; font-size: 13px; color: var(--muted); min-width: 0; }
.kv b { color: var(--ink); font-weight: 550; overflow-wrap: anywhere; }
.commits { margin: 8px 0 0; padding-left: 18px; font-size: 13px; display: grid; gap: 4px; }
.commits code { font: 12px var(--mono); color: var(--muted); }
.counts { display: flex; gap: 8px; flex-wrap: wrap; }

/* lists of sessions */
.runs li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 16px; padding: 10px 0;
  border-top: 1px solid var(--line); }
.runs li:first-child { border-top: 0; }
.runs a { font-weight: 550; overflow-wrap: anywhere; }
.runs .meta { grid-column: 1 / -1; font: 12px var(--mono); color: var(--muted); overflow-wrap: anywhere; }
.runs .when { font-size: 12px; color: var(--muted); white-space: nowrap; }
.tag { display: inline-flex; align-items: center; height: 20px; padding: 0 7px; border-radius: 5px; background: var(--sunk);
  border: 1px solid var(--line); font: 600 11.5px var(--mono); color: var(--ink-2); white-space: nowrap; vertical-align: middle; }
.ses { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 14px; padding: 11px 0; border-top: 1px solid var(--line); }
.ses:first-child { border-top: 0; }
.ses .t { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.ses .t a { font-weight: 600; overflow-wrap: anywhere; }
.ses .meta { grid-column: 1 / -1; font-size: 12px; color: var(--muted); display: flex; gap: 4px 14px; flex-wrap: wrap; }
.ses .meta code { font-size: 11.5px; }
.ticket { border-top: 1px solid var(--line); padding-top: 12px; }
.ticket:first-of-type { border-top: 0; padding-top: 0; }
.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 16px; font-size: 13px; margin: 0; }
.facts dt { color: var(--muted); } .facts dd { margin: 0; font-family: var(--mono); font-size: 12.5px; overflow-wrap: anywhere; }

/* transcripts */
#msgs { display: flex; flex-direction: column; gap: 12px; }
.msg { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column;
  gap: 10px; min-width: 0; background: var(--panel); }
.msg.user { background: var(--user-soft); border-color: var(--user-line); }
.msg .who { font: 12px var(--mono); color: var(--muted); display: flex; gap: 4px 8px; flex-wrap: wrap; align-items: center; }
.msg .who b { color: var(--ink); font-family: var(--sans); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.msg.user .who b { color: var(--user); }
.msg.assistant .who b { color: var(--ok); }
.txt { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; line-height: 1.55; }
details.tool, details.think { background: var(--sunk); border: 1px solid var(--line); border-radius: 8px;
  padding: 7px 11px; min-width: 0; }
details.think { background: transparent; border-style: dashed; }
details.think summary { color: var(--muted); font-style: italic; }
details summary { cursor: pointer; font: 12.5px/1.6 var(--mono); overflow-wrap: anywhere; }
details.tool summary b { color: var(--tool); font-weight: 650; }
details summary .st { color: var(--muted); }
details summary .ok { color: var(--ok); }
details summary .bad { color: var(--bad); }
pre.io { white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.5 var(--mono); margin: 8px 0 0;
  max-height: 480px; overflow: auto; background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; }
.lbl { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin-top: 8px; }
.send { display: flex; flex-direction: column; gap: 8px; margin: 0; }
.send button { align-self: flex-start; }
.inbox li { font: 12px var(--mono); color: var(--muted); padding: 3px 0; overflow-wrap: anywhere; }
.inbox .bad { color: var(--bad); }
.diff .add { color: var(--ok); background: var(--ok-soft); display: inline-block; min-width: 100%; }
.diff .del { color: var(--bad); background: var(--bad-soft); display: inline-block; min-width: 100%; }
.diff .hunk { color: var(--accent); }
.todos { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 4px; font-size: 14px; }
.todos li { display: grid; grid-template-columns: 1.2em minmax(0, 1fr); gap: 6px; overflow-wrap: anywhere; }
.todos .completed { color: var(--muted); text-decoration: line-through; }
.todos .in_progress { font-weight: 650; color: var(--run); }
.todos .cancelled { color: var(--muted); }

/* the ticket page */
.qs { display: flex; flex-direction: column; gap: 12px; }
.qs label { display: flex; flex-direction: column; gap: 6px; }
.qs .q { font-weight: 550; overflow-wrap: anywhere; }
.qs .q.blocking::before { content: "blocking"; color: var(--wait); background: var(--wait-soft); font-size: 11px;
  font-weight: 700; padding: 1px 6px; border-radius: 4px; margin-right: 8px; vertical-align: 1px; }
.acts { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.acts form { margin: 0; display: inline-flex; flex-wrap: wrap; gap: 6px; align-items: center; max-width: 100%; }
.acts label { display: inline-flex; gap: 6px; align-items: center; }
.acts .sep { width: 1px; align-self: stretch; background: var(--line); margin: 0 4px; }
.attempts { list-style: none; padding: 0; margin: 0; }
.attempts > li { padding: 9px 0; border-top: 1px solid var(--line); font-size: 13px; overflow-wrap: anywhere;
  display: flex; flex-direction: column; gap: 4px; }
.attempts > li:first-child { border-top: 0; }
.attempts .hd { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.attempts ul { margin: 0; padding-left: 18px; color: var(--ink-2); list-style: disc; }
.notes { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 10px; }
.notes li { border-left: 3px solid var(--accent); background: var(--sunk); border-radius: 0 8px 8px 0; padding: 8px 12px;
  font-size: 14px; }
.notes .at { font-size: 11.5px; color: var(--muted); display: block; margin-bottom: 2px; }
.notes .txt { font-size: 14px; }
.flash { background: var(--ok-soft); color: var(--ink); border: 1px solid color-mix(in srgb, var(--ok) 35%, transparent);
  border-radius: 9px; padding: 10px 14px; margin: 0; overflow-wrap: anywhere; }
.flash.bad { background: var(--bad-soft); border-color: color-mix(in srgb, var(--bad) 35%, transparent); }
pre.qlog { white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.55 var(--mono); margin: 10px 0 0; max-height: 560px;
  overflow: auto; background: var(--sunk); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
details.doc summary { font: 600 14px var(--sans); display: flex; align-items: center; gap: 8px; }
details.doc summary .muted { font-weight: 400; }
.wide { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 360px); gap: 18px; align-items: start; }
.wide2 { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr); gap: 18px; align-items: start; }
@media (max-width: 1020px) { .wide, .wide2 { grid-template-columns: minmax(0, 1fr); } }
/* A long transcript scrolls; the session's side panel (message box, Stop, todo list,
   facts) and a ticket's buttons stay in view, under the top bar. */
@media (min-width: 821px) {
  .cols > .stick { position: sticky; top: 68px; max-height: calc(100vh - 84px); overflow-y: auto;
    overscroll-behavior: contain; scrollbar-width: thin; border-radius: 12px; }
  #r-acts { position: sticky; top: 60px; z-index: 10; }
}
#add { scroll-margin-top: 68px; transition: box-shadow .3s; }
#add.hl { box-shadow: 0 0 0 2px var(--accent); }
@media (max-width: 1180px) {
  .tk { grid-template-columns: 22px minmax(0, 1fr) auto; padding: 12px 14px; }
  .tk .strip { grid-column: 2 / -1; grid-row: 2; max-width: 520px; }
  .tk .when { grid-column: 3; grid-row: 1; align-self: start; }
  .tk .ra { grid-column: 2 / -1; grid-row: 3; justify-content: flex-start; }
}
@media (max-width: 860px) {
  .presets { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .presets.three { grid-template-columns: minmax(0, 1fr); }
  .runner { grid-template-columns: minmax(0, 1fr); } .runner .startf { justify-content: flex-start; }
}
@media (max-width: 560px) {
  .grid2, .presets.two { grid-template-columns: minmax(0, 1fr); }
  .runchip .rtxt { display: none; }
  .tabs { gap: 0; flex-shrink: 0; } .tabs a { padding: 6px 6px; font-size: 13px; } .bar-mid { gap: 6px; }
  .bar-in { gap: 4px 8px; height: auto; min-height: 52px; flex-wrap: wrap; padding-top: 8px; padding-bottom: 6px; }
  .bar-mid { order: 3; flex-basis: 100%; overflow-x: auto; }
  .bar-who span { display: none; }
  .fresh { max-width: 22ch; margin-left: auto; }
  .tk .pos { display: none; } .tk { grid-template-columns: minmax(0, 1fr) auto; }
  .tk .strip, .tk .ra { grid-column: 1 / -1; } .tk .when { grid-column: 2; }
  main.rv { padding-top: 16px; }
  .panel { padding: 14px; }
  .panel.flush > .hd { padding: 12px 14px; }
}
"""


def _ms(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _when(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000)) if ms else ""


def _ago(ms, now=None):
    s = max(0, int((now or time.time()) - ms / 1000))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _dur(ms):
    s = ms / 1000
    return f"{s:.1f}s" if s < 90 else f"{int(s // 60)}m{int(s % 60):02d}s"


def _loads(text):
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _norm(path):
    return os.path.normcase(os.path.normpath(path.replace("\\", "/"))) if path else ""


def _cap(text, limit=TEXT_CAP):
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _model_name(raw):
    m = _loads(raw) if isinstance(raw, str) else (raw or {})
    mid = m.get("modelID") or m.get("id") or ""
    prov = m.get("providerID") or ""
    return f"{prov}/{mid}" if prov and mid else mid


class Source:
    def __init__(self, label, data_dir):
        self.label = label
        self.db_path = os.path.join(data_dir, "opencode", "opencode.db")
        self.live_dir = os.path.join(data_dir, "harness", "live")

    def connect(self):
        if not os.path.isfile(self.db_path):
            return None
        uri = "file:" + quote(os.path.abspath(self.db_path).replace("\\", "/")) + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=3)
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.Error:
            return None


class Runs:
    def __init__(self, sources, queue_dir="", vault="", harness="", project_dir=""):
        """sources: (label, data dir) pairs; queue_dir: harness queue's state/queue;
        vault: the active vault (the plans, reviews and answers are there): a path,
        or an object whose get() gives (path, name) now;
        harness: the harness script the Start button runs `queue run` with;
        project_dir: the folder harness serve was started in."""
        self.sources = [Source(label, d) for label, d in sources]
        self.queue_dir = queue_dir
        self._vault = vault
        self.harness = harness or HARNESS
        self.project_dir = project_dir
        self.launched = None        # the `harness queue run` this viewer started: (Popen, started at)
        self._in_place = {}         # KEY: (state.json stat, project, launch windows) of in-place tickets
        self.launch_lock = threading.RLock()

    @property
    def vault(self):
        return self._vault.get()[0] if hasattr(self._vault, "get") else self._vault

    # --- reading -------------------------------------------------------------

    def _query(self, src, sql, args=()):
        con = src.connect()
        if con is None:
            return []
        try:
            return con.execute(sql, args).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def live(self, now=None):
        """{session id: (source, marker age seconds)} of runs whose relay is alive."""
        now = now or time.time()
        out = {}
        for src in self.sources:
            try:
                names = os.listdir(src.live_dir)
            except OSError:
                continue
            for name in names:
                sid = name[:-5]
                if not name.endswith(".json") or not SID_RE.match(sid):
                    continue
                try:
                    age = now - os.path.getmtime(os.path.join(src.live_dir, name))
                except OSError:
                    continue
                if age <= LIVE_FRESH:
                    out[sid] = (src, age)
        return out

    def sessions(self, roots_only=True, limit=LIST_CAP):
        rows = []
        where = "where s.parent_id is null and s.time_archived is null" if roots_only else "where s.time_archived is null"
        for src in self.sources:
            for r in self._query(src, "select s.id, s.parent_id, s.directory, s.title, s.agent, s.model, "
                                      "s.time_created, s.time_updated, s.tokens_input, s.tokens_output, s.cost, "
                                      "(select count(*) from session c where c.parent_id = s.id) as kids "
                                      f"from session s {where} order by s.time_updated desc limit ?", (limit,)):
                d = dict(r)
                d["source"] = src
                rows.append(d)
        rows.sort(key=lambda d: _ms(d["time_updated"]), reverse=True)
        return rows[:limit]

    def find(self, sid):
        """(source, session row as dict) or (None, None)."""
        if not SID_RE.match(sid or ""):
            return None, None
        for src in self.sources:
            rows = self._query(src, "select * from session where id = ?", (sid,))
            if rows:
                return src, dict(rows[0])
        return None, None

    def children(self, src, sid):
        return [dict(r) for r in self._query(
            src, "select id, title, agent, time_created, time_updated from session where parent_id = ? "
                 "order by time_created", (sid,))]

    def messages(self, src, sid):
        """[(message id, info dict, time_created, [part dicts])] in order."""
        msgs = [(r["id"], _loads(r["data"]), _ms(r["time_created"]), [])
                for r in self._query(src, "select id, data, time_created from message where session_id = ? "
                                          "order by time_created, id", (sid,))]
        index = {m[0]: m[3] for m in msgs}
        for r in self._query(src, "select id, message_id, data from part where session_id = ? order by id", (sid,)):
            if r["message_id"] in index:
                p = _loads(r["data"])
                p["id"] = r["id"]
                index[r["message_id"]].append(p)
        return msgs

    def part(self, src, sid, pid):
        rows = self._query(src, "select data from part where session_id = ? and id = ?", (sid, pid))
        return _loads(rows[0]["data"]) if rows else None

    def root_of(self, src, row):
        """The top session of row's subagent chain."""
        seen = set()
        while row and row.get("parent_id") and row["id"] not in seen:
            seen.add(row["id"])
            rows = self._query(src, "select * from session where id = ?", (row["parent_id"],))
            if not rows:
                break
            row = dict(rows[0])
        return row

    def chain(self, src, row):
        """The parent sessions above row, top first."""
        out, seen = [], set()
        while row and row.get("parent_id") and row["id"] not in seen:
            seen.add(row["id"])
            rows = self._query(src, "select id, parent_id, title from session where id = ?", (row["parent_id"],))
            if not rows:
                break
            row = dict(rows[0])
            out.append(row)
        return out[::-1]

    # --- the queue -----------------------------------------------------------

    def ticket_of_row(self, row):
        """ticket_of for a session row: its folder and when it was made."""
        return self.ticket_of(row.get("directory") or "", _ms(row.get("time_created")))

    def ticket_of(self, directory, created=0):
        """The queue ticket KEY whose clone (state/queue/KEY/repo) directory is in, or
        "". An in-place ticket's agents work in the project itself: a session there
        made (created, ms) while one of the ticket's stages ran is the ticket's."""
        if not self.queue_dir or not directory:
            return ""
        q, d = _norm(self.queue_dir), _norm(directory)
        prefix = q.rstrip("/\\") + os.sep
        if not d.startswith(os.path.normcase(prefix)):
            return self._in_place_ticket(d, created) if created else ""
        rest = d[len(prefix):].replace("\\", "/").split("/")
        if len(rest) >= 2 and rest[1] == "repo":
            # the KEY as the queue spelled it, not normcase'd
            for name in self._ticket_dirs():
                if os.path.normcase(name) == rest[0]:
                    return name
        return ""

    def _in_place_ticket(self, d, created):
        for key in self._ticket_dirs():
            path = os.path.join(self.queue_dir, key, "state.json")
            try:
                fs = os.stat(path)
                mtime = (fs.st_mtime_ns, fs.st_size, fs.st_ino)   # the queue replaces it: a new inode
            except OSError:
                continue
            got = self._in_place.get(key)
            if not got or got[0] != mtime:
                try:
                    with open(path, encoding="utf-8") as f:
                        st = json.load(f)
                except (OSError, ValueError):
                    st = {}
                st = st if isinstance(st, dict) else {}
                wins = [w for w in st.get("launches") or [] if isinstance(w, list) and len(w) == 2
                        and all(isinstance(x, int) for x in w)] if st.get("in_place") is True else []
                proj = st.get("project")
                got = self._in_place[key] = (mtime, _norm(proj) if isinstance(proj, str) else "", wins)
            if got[1] == d and any(a <= created and (not b or created <= b) for a, b in got[2]):
                return key
        return ""

    def _ticket_dirs(self):
        try:
            return [n for n in os.listdir(self.queue_dir) if KEY_RE.match(n)]
        except OSError:
            return []

    def ticket_keys(self):
        """The queue's tickets in its order, then any other ticket folder."""
        dirs = self._ticket_dirs()
        try:
            with open(os.path.join(self.queue_dir, "queue.list"), encoding="utf-8") as f:
                order = [k for k in f.read().split() if k in dirs]
        except OSError:
            order = []
        return order + sorted(k for k in dirs if k not in order)

    def ticket_state(self, key):
        """state.json, its fields checked: written by the queue, but a hand edit or
        an old version must not take the page down. The owner's notes are read from
        notes.json (only `note` writes it; state.json carries a copy)."""
        try:
            with open(os.path.join(self.queue_dir, key, "state.json"), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(st, dict):
            return {}
        out = {k: st[k] for k in ("status", "stage", "reason", "updated", "vault", "project", "base", "ref",
                                  "start", "branch", "sha", "created", "stopped_from") if isinstance(st.get(k), str)}
        out.setdefault("start", out.get("ref", ""))
        out["attempt"] = st["attempt"] if isinstance(st.get("attempt"), int) else 0
        out["plan_approved"] = st.get("plan_approved") is True
        out["in_place"] = st.get("in_place") is True
        out["plan_review"] = st.get("plan_review") is True
        want = st.get("stages")
        want = [s for s in want if isinstance(s, str)] if isinstance(want, list) and want else list(STAGES)
        out["stages"] = [s for s in STAGES if s in want]
        notes = st.get("notes")
        try:
            with open(os.path.join(self.queue_dir, key, "notes.json"), encoding="utf-8") as f:
                notes = json.load(f)
        except (OSError, ValueError):
            pass
        out["notes"] = [{"at": str(n.get("at", "")), "text": n["text"]} for n in
                        (notes if isinstance(notes, list) else []) if isinstance(n, dict) and isinstance(n.get("text"), str)]
        qs = st.get("questions")
        out["questions"] = [{"text": q["text"], "blocking": bool(q.get("blocking"))} for q in
                            (qs if isinstance(qs, list) else []) if isinstance(q, dict) and isinstance(q.get("text"), str)]
        hist = st.get("history")
        out["history"] = [{"at": str(h.get("at", "")), "stage": str(h.get("stage", "")),
                           "attempt": h["attempt"] if isinstance(h.get("attempt"), int) else 0,
                           "result": str(h.get("result", "")),
                           "details": [x for x in h.get("details") or [] if isinstance(x, str)]
                           if isinstance(h.get("details"), list) else []}
                          for h in (hist if isinstance(hist, list) else []) if isinstance(h, dict)]
        push = st.get("push")
        if isinstance(push, dict) and push.get("result") in ("pushed", "local", "rejected", "skipped"):
            keys = ("result", "remote", "url", "branch", "sha", "error", "repo")
            out["push"] = {k: str(push.get(k) or "") for k in keys}
            pushes = st.get("pushes")
            out["pushes"] = [{k: str(p.get(k) or "") for k in keys} for p in
                             (pushes if isinstance(pushes, list) else [])
                             if isinstance(p, dict) and p.get("result") in PUSHED]
            left = st.get("left_uncommitted")
            out["left_uncommitted"] = [r for r in left if isinstance(r, str)] if isinstance(left, list) else []
            url = st.get("compare_url")
            out["compare_url"] = url if isinstance(url, str) and re.match(r"https?://", url) else ""
            commits = st.get("commits")
            out["commits"] = [{"sha": str(c.get("sha", "")), "subject": str(c.get("subject", ""))} for c in
                              (commits if isinstance(commits, list) else []) if isinstance(c, dict)]
            stat = st.get("diffstat") if isinstance(st.get("diffstat"), dict) else {}
            out["diffstat"] = {k: stat[k] if isinstance(stat.get(k), int) else 0
                               for k in ("files", "insertions", "deletions")}
            out["warnings"] = [w for w in st.get("warnings") or [] if isinstance(w, str)] \
                if isinstance(st.get("warnings"), list) else []
            now_on = st.get("folder_branch_now")
            out["folder_branch_now"] = now_on if isinstance(now_on, str) else ""
        # the owner asked a running ticket to stop; the run has not got to it yet
        out["stopping"] = out.get("status") == "running" and os.path.exists(os.path.join(self.queue_dir, key, "stop"))
        if out["stopping"]:
            try:
                out["stop_wait"] = time.time() - os.path.getmtime(os.path.join(self.queue_dir, key, "stop"))
            except OSError:
                out["stop_wait"] = 0
        if out.get("status") == "running":
            r = self.runner()
            if r["running"] and r.get("ticket") == key:
                out["step"] = r.get("step", "")
            elif not r["starting"]:
                # left running by a run that ended without putting it back (killed, its
                # window closed): a run takes it again at its stage
                out.update(status="interrupted", stopping=False, run_going=r["running"])
        return out

    def retry_choices(self, key):
        """{stage: why the queue refuses a retry there, or ""}, None the plain retry:
        ticket_queue.retry_choices on state.json as it is (the remotes as last
        fetched). None when the queue's module or the state cannot be read."""
        tq = _tq()
        if tq is None:
            return None
        try:
            q = tq.Queue(self.queue_dir)
            st = q.load(key)
            return tq.retry_choices(q, st) if isinstance(st, dict) else None
        except Exception:   # a hand-edited state.json: every stage, and the queue says no where it must
            return None

    def vault_of(self, st):
        """The vault the queue ran the ticket with (it may not be the one this
        viewer started with), else this viewer's."""
        v = st.get("vault")
        return v if isinstance(v, str) and os.path.isdir(v) else self.vault

    def note(self, key, kind, vault=None):
        """The ticket's KEY-<kind>.md in the vault (plan, review, answers), or ""."""
        vault = vault or self.vault
        if not vault:
            return ""
        try:
            with open(os.path.join(vault, "wiki", "notes", "tickets", f"{key}-{kind}.md"),
                      encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def stage_outs(self, key):
        """[(name, mtime, size)] of the ticket's stage outputs, oldest first."""
        d = os.path.join(self.queue_dir, key)
        out = []
        try:
            for n in os.listdir(d):
                if OUT_RE.match(n):
                    st = os.stat(os.path.join(d, n))
                    out.append((n, st.st_mtime, st.st_size))
        except OSError:
            return []
        return sorted(out, key=lambda t: t[1])

    def stage_of(self, key, created_ms, outs=None):
        """The stage (e.g. plan-1) a ticket session belongs to: the first stage output
        written at or after the session started. A running stage has no output yet
        finished, so the newest session past every output is the stage in progress."""
        for name, mtime, _ in (self.stage_outs(key) if outs is None else outs):
            if mtime * 1000 >= created_ms:
                return name[:-4]
        return ""

    # --- sending -------------------------------------------------------------

    def send(self, sid, text, sender="local"):
        """Drop a message in the live run's inbox. Returns (ok, message)."""
        if not text.strip():
            return False, "the message is empty"
        if len(text.encode()) > MAX_MESSAGE:
            return False, f"the message is over {MAX_MESSAGE} bytes"
        live = self.live().get(sid)
        if not live:
            return False, "this run is not live (finished, or not a headless run)"
        src = live[0]
        body = {"parts": [{"type": "text", "text": text}]}
        # The session's own agent and model: without them opencode answers with its
        # default agent ("build"), not the run's (e.g. yolo).
        msgs = self._query(src, "select data from message where session_id = ? order by time_created desc, id desc",
                           (sid,))
        for r in msgs:
            info = _loads(r["data"])
            if info.get("role") == "user":
                if info.get("agent"):
                    body["agent"] = info["agent"]
                m = info.get("model") or {}
                if m.get("providerID") and m.get("modelID"):
                    body["model"] = {"providerID": m["providerID"], "modelID": m["modelID"]}
                break
        inbox = os.path.join(src.live_dir, sid + ".inbox")
        sender = re.sub(r"[^A-Za-z0-9_.]", "_", sender)[:32] or "local"
        name = f"{int(time.time() * 1000)}-{sender}-{secrets.token_hex(3)}"
        tmp = os.path.join(inbox, name + ".tmp")
        try:
            os.makedirs(inbox, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(body, f)
            os.replace(tmp, os.path.join(inbox, name + ".json"))
        except OSError as e:
            return False, f"cannot write the run's inbox: {e.strerror or e}"
        return True, "queued"

    def inbox(self, src, sid):
        """[(sent ms, sender, state, text)] newest first; state: waiting, sent, failed-N, late."""
        d = os.path.join(src.live_dir, sid + ".inbox")
        out = []
        try:
            names = os.listdir(d)
        except OSError:
            return []
        for n in names:
            base, _, state = n.rpartition(".")
            if state == "tmp" or not base:
                continue
            ts, _, rest = base.partition("-")
            sender = rest.rsplit("-", 1)[0]
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    body = json.load(f)
                text = "".join(p.get("text", "") for p in body.get("parts", []) if isinstance(p, dict))
            except (OSError, ValueError, AttributeError):
                text = ""
            out.append((_ms(ts), sender, "waiting" if state == "json" else state, text))
        return sorted(out, reverse=True)

    # --- the queue's runner and sessions ----------------------------------------

    def ticket_sessions(self, key):
        """Every top session run in the ticket's clone (or, in place, in the project
        while its stages ran), from every source, archived ones too, oldest first."""
        out = []
        st = self.ticket_state(key)
        # the project's folder name: its path may be spelled C:/... or /c/...
        needle = os.path.basename(st["project"].replace("\\", "/").rstrip("/")) if st.get("in_place") and st.get("project") else key
        needle = needle or key
        for src in self.sources:
            for r in self._query(src, "select s.id, s.parent_id, s.directory, s.title, s.agent, s.model, "
                                      "s.time_created, s.time_updated, s.tokens_input, s.tokens_output, s.cost, "
                                      "(select count(*) from session c where c.parent_id = s.id) as kids "
                                      "from session s where s.parent_id is null and instr(s.directory, ?) > 0 "
                                      "order by s.time_created limit 500", (needle,)):
                d = dict(r)
                if self.ticket_of_row(d) == key:
                    d["source"] = src
                    out.append(d)
        out.sort(key=lambda d: _ms(d["time_created"]))
        return out

    def runner(self, now=None):
        """What the queue's run is doing: run.json (the run beats it every few
        seconds; a beat older than RUN_FRESH is a run that is gone), and the run this
        viewer started, if any: "starting" until its first beat, or how it ended."""
        now = now or time.time()
        info = {}
        try:
            with open(os.path.join(self.queue_dir, "run.json"), encoding="utf-8") as f:
                got = json.load(f)
            if isinstance(got, dict):
                info = got
        except (OSError, ValueError):
            pass
        out = {k: info[k] for k in ("model", "ticket", "stage", "step", "started") if isinstance(info.get(k), str)}
        out.update(host=info.get("host") is True, single=info.get("single") is True)
        beat = info.get("beat")
        out["beat"] = beat if isinstance(beat, (int, float)) and not isinstance(beat, bool) else 0
        out["pid"] = info.get("pid") if isinstance(info.get("pid"), int) else 0
        out["running"] = bool(out["beat"]) and now - out["beat"] < RUN_FRESH
        out["halting"] = out["running"] and os.path.exists(os.path.join(self.queue_dir, "halt"))
        out["starting"] = False
        out["exited"] = None
        with self.launch_lock:
            if self.launched:
                proc, at = self.launched
                rc = proc.poll()
                if rc is None and not out["running"]:
                    out["starting"] = True
                    out["since"] = at
                elif rc is not None and not out["running"] and now - at < 3600:
                    out["exited"] = (rc, at)
        return out

    def runner_out(self, lines=12):
        """The last lines `harness queue run` wrote to runner.out, when this viewer started it."""
        try:
            with open(os.path.join(self.queue_dir, "runner.out"), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 16384))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return ""
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
        return "\n".join([x for x in text.splitlines() if x.strip()][-lines:])

    def start_runner(self, mode):
        """Start `harness queue run` in the background, as its own process group with
        no terminal. mode: container, host or single (single-message host mode).
        Returns (ok, message)."""
        if mode not in ("container", "host", "single"):
            return False, "pick container, host or single-message mode"
        if not self.queue_dir:
            return False, "this viewer has no queue (pub.py --queue-dir)"
        if not os.path.isfile(self.harness):
            return False, f"no harness script at {self.harness} (pub.py --harness)"
        bash = shutil.which("bash")
        if not bash:
            return False, "bash is not on PATH, so harness cannot run"
        with self.launch_lock:
            r = self.runner()
            if r["running"]:
                return False, "the queue is already running"
            if self.launched and self.launched[0].poll() is None:
                return False, "the queue is starting already"
            argv = [bash, self.harness, "queue", "run"]
            if mode == "host":
                argv.append("--host")
            elif mode == "single":
                argv.append("--single-message")   # harness takes it for host mode too
            env = dict(os.environ)
            env["HARNESS_OPEN"] = "0"             # no browser tab: the owner is on this page
            if mode != "container":
                # Host mode asks for a yes on the terminal; there is none. The owner ticked
                # the box that says the same thing on the queue page.
                env["HARNESS_HOST_CONFIRM"] = "1"
            parent = os.path.dirname(os.path.abspath(self.queue_dir))
            if not env.get("HARNESS_INSTALL_ROOT") and os.path.basename(parent) == "state":
                # the run must use the queue this page shows: <install root>/state/queue
                env["HARNESS_INSTALL_ROOT"] = os.path.dirname(parent)
            cwd = os.path.expanduser("~")
            if not os.path.isdir(cwd):
                cwd = parent
            kw = {}
            if os.name == "nt":
                kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                kw["start_new_session"] = True
            try:
                os.makedirs(self.queue_dir, exist_ok=True)
                with open(os.path.join(self.queue_dir, "runner.out"), "ab") as out:
                    out.write(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} started from the queue page: "
                              f"harness queue run {' '.join(argv[4:])}\n".encode())
                    out.flush()
                    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                            cwd=cwd, env=env, **kw)
            except OSError as e:
                return False, f"cannot start harness: {e.strerror or e}"
            self.launched = (proc, time.time())
        # A run that fails its checks (no vault, no upstream login, no docker for
        # container mode) ends within seconds; say why rather than "started".
        for _ in range(30):
            if proc.poll() is not None or self.runner()["running"]:
                break
            time.sleep(0.1)
        if proc.poll() is not None:
            last = self.runner_out(1)
            return False, f"harness queue run ended at once (status {proc.returncode})" + (f": {last}" if last else "")
        return True, "the queue run is starting" + (" in host mode" if mode != "container" else "")


# --- rendering: messages ------------------------------------------------------

TODO_MARK = {"completed": "&#10003;", "in_progress": "&#9656;", "cancelled": "&#10007;"}


def _todos(todos):
    parts = ["<ul class='todos'>"]
    for t in todos:
        if not isinstance(t, dict):
            continue
        status = t.get("status") if isinstance(t.get("status"), str) else "pending"
        parts.append(f"<li class='{esc(status)}'><span>{TODO_MARK.get(status, '&#9675;')}</span>"
                     f"<span>{esc(str(t.get('content') or ''))}</span></li>")
    return "".join(parts) + "</ul>"


def todos_of(msgs):
    """The session's todo list as its last todowrite left it, or None."""
    last = None
    for _, _, _, plist in msgs:
        for p in plist:
            if p.get("type") == "tool" and p.get("tool") == "todowrite":
                st = p.get("state") or {}
                t = (st.get("metadata") or {}).get("todos") or (st.get("input") or {}).get("todos")
                if isinstance(t, list):
                    last = t
    return last


def _diff(text):
    out = []
    for line in text.splitlines():
        cls = ("add" if line.startswith("+") and not line.startswith("+++") else
               "del" if line.startswith("-") and not line.startswith("---") else
               "hunk" if line.startswith("@@") else "")
        out.append(f"<span class='{cls}'>{esc(line)}</span>" if cls else esc(line))
    return "<pre class='io diff'>" + "\n".join(out) + "</pre>"


def _tool(p, sid):
    st = p.get("state") or {}
    status = st.get("status") or "?"
    name = p.get("tool") or "tool"
    title = st.get("title") or ""
    t = st.get("time") or {}
    took = _dur(_ms(t.get("end")) - _ms(t.get("start"))) if t.get("end") and t.get("start") else ""
    cls = "bad" if status == "error" else "st"
    meta = st.get("metadata") or {}
    inp = st.get("input")
    # A file change shows as its diff (edit, apply_patch: opencode keeps a unified
    # diff in the metadata; write: the new contents), open, as opencode shows it.
    diff = meta.get("diff") if isinstance(meta.get("diff"), str) else ""
    if not diff and name == "write" and isinstance(inp, dict) and isinstance(inp.get("content"), str):
        diff = "".join("+" + line + "\n" for line in inp["content"].splitlines())
    counts = ""
    if diff:
        lines = diff.splitlines()
        plus = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
        minus = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
        counts = f" <span class='st'>+{plus} &minus;{minus}</span>"
    head = (f"<summary><b>{esc(name)}</b> {esc(title)}{counts} <span class='{cls}'>{esc(status)}"
            f"{' ' + esc(took) if took else ''}</span></summary>")
    body = []
    child = meta.get("sessionId") or meta.get("sessionID")
    if isinstance(child, str) and SID_RE.match(child):
        body.append(f"<div><a href='/runs/{quote(child)}'>open the subagent session &rarr;</a></div>")
    if name == "todowrite" and isinstance(inp, dict) and isinstance(inp.get("todos"), list):
        return f"<details class='tool' open>{head}{_todos(inp['todos'])}</details>"
    if diff and status != "error":
        text, cut = _cap(diff)
        body.append(f"<div class='lbl'>diff</div>{_diff(text)}" + (" &hellip;" if cut else ""))
        if isinstance(inp, dict):
            inp = {k: v for k, v in inp.items() if k in ("filePath", "path", "replaceAll")}
    if inp:
        text, cut = _cap(json.dumps(inp, indent=2, ensure_ascii=False))
        body.append(f"<div class='lbl'>input</div><pre class='io'>{esc(text)}{' &hellip;' if cut else ''}</pre>")
    out = st.get("output") if status != "error" else st.get("error")
    if out:
        out = out if isinstance(out, str) else json.dumps(out, indent=2, ensure_ascii=False)
        text, cut = _cap(out)
        body.append(f"<div class='lbl'>{'error' if status == 'error' else 'output'}</div>"
                    f"<pre class='io'>{esc(text)}</pre>")
        if cut:
            body.append(f"<div class='muted'>{len(out):,} characters; "
                        f"<a href='/runs/{quote(sid)}/part/{quote(p['id'])}' target='_blank' rel='noopener'>"
                        "the whole output</a></div>")
    return f"<details class='tool'{' open' if diff and status != 'error' else ''}>{head}{''.join(body)}</details>"


def _part(p, sid):
    kind = p.get("type")
    if kind == "text":
        if p.get("synthetic") or not (p.get("text") or "").strip():
            return ""
        return f"<div class='txt'>{esc(p['text'])}</div>"
    if kind == "reasoning":
        text = (p.get("text") or "").strip()
        return f"<details class='think'><summary>thinking</summary><div class='txt'>{esc(text)}</div></details>" if text else ""
    if kind == "tool":
        return _tool(p, sid)
    if kind == "file":
        return f"<div class='muted'>file: {esc(p.get('filename') or p.get('url') or '')} {esc(p.get('mime') or '')}</div>"
    if kind == "patch":
        files = p.get("files") or []
        return f"<div class='muted'>changed: {esc(', '.join(str(f) for f in files[:20]))}</div>" if files else ""
    if kind == "subtask":
        return (f"<div class='muted'>subtask for <b>{esc(p.get('agent') or '')}</b>: "
                f"{esc(p.get('description') or '')}</div>")
    if kind == "agent":
        return f"<div class='muted'>agent: {esc(p.get('name') or '')}</div>"
    if kind == "compaction":
        return "<div class='muted'>(context compacted)</div>"
    if kind == "retry":
        return f"<div class='muted'>retry: {esc(str((p.get('error') or {}).get('data', {}).get('message', '')))}</div>"
    return ""


def render_messages(msgs, sid, start=0):
    parts = []
    for i, (mid, info, created, plist) in enumerate(msgs[start:], start):
        role = info.get("role") or "?"
        who = [f"<b>{esc(role)}</b>"]
        if info.get("agent"):
            who.append(esc(info["agent"]))
        model = _model_name(info.get("model")) or _model_name({"providerID": info.get("providerID"),
                                                                 "modelID": info.get("modelID")})
        if model:
            who.append(esc(model))
        who.append(esc(_when(created)))
        tok = info.get("tokens") or {}
        if role == "assistant" and (tok.get("input") or tok.get("output")):
            who.append(f"{_ms(tok.get('input')):,} in / {_ms(tok.get('output')):,} out")
        body = "".join(_part(p, sid) for p in plist)
        err = info.get("error")
        if err:
            msg = (err.get("data") or {}).get("message") or err.get("name") or ""
            body += f"<div class='err'>{esc(str(msg))}</div>"
        if role == "assistant" and not body and not info.get("time", {}).get("completed"):
            body = "<div class='muted'>working&hellip;</div>"
        parts.append(f"<div class='msg {esc(role)}' data-i='{i}'><div class='who'>{' &middot; '.join(who)}</div>"
                     f"{body}</div>")
    return "".join(parts)


# --- rendering: shared pieces ---------------------------------------------------

def _iso_ms(text):
    """The queue's ISO UTC stamps (2026-01-02T03:04:05Z) as epoch ms, 0 if not one."""
    try:
        return calendar.timegm(time.strptime((text or "")[:19], "%Y-%m-%dT%H:%M:%S")) * 1000
    except ValueError:
        return 0


def _sig(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()


PILL = {"running": ("t-run dot pulse", "running"), "needs-answers": ("t-wait dot", "needs your answers"),
        "plan-review": ("t-wait dot", "plan to approve"), "blocked": ("t-bad dot", "blocked"),
        "done": ("t-ok dot", "done"), "stopped": ("t-off dot", "stopped"), "queued": ("t-queued", "queued"),
        "interrupted": ("t-wait dot", "run ended")}
TONE = {"running": "run", "needs-answers": "wait", "plan-review": "wait", "blocked": "bad", "stopped": "stopped",
        "queued": "queued", "interrupted": "wait"}


def _pill(status, stopping=False):
    if stopping:
        return "<span class='pill t-run dot'>stopping</span>"
    cls, label = PILL.get(status, ("t-off", status or "?"))
    return f"<span class='pill {cls}'>{esc(label)}</span>"


def _status(runs, row, live, now=None):
    now = now or time.time()
    if row["id"] in live:
        return "<span class='pill t-run dot pulse'>live</span>"
    if now - _ms(row["time_updated"]) / 1000 <= ACTIVE:
        return "<span class='pill t-acc dot'>active</span>"
    return "<span class='pill t-off'>idle</span>"


def _flash(msg, bad=False):
    return f"<p class='flash{' bad' if bad else ''}' role='status'>{esc(msg)}</p>" if msg else ""


def stage_strip(st, big=False):
    """plan implement review fix push, each done, skipped, current (in the ticket's
    status colour), still to do, or not one of the ticket's stages."""
    stages, status, cur = st.get("stages") or list(STAGES), st.get("status", ""), st.get("stage", "")
    last = {}
    for h in st.get("history") or []:
        last[h["stage"]] = h
    done = status == "done" or cur == "done"
    at = STAGES.index(cur) if cur in STAGES else (len(STAGES) if done else 0)
    items = []
    for i, s in enumerate(STAGES):
        h = last.get(s)
        if s not in stages:
            cls, note = "off", "not in this ticket"
        elif i < at:
            cls, note = ("done", "passed") if h and h["result"] == "passed" else ("skip", "not needed" if s == "fix" else "skipped")
        elif i == at:
            cls = "cur " + TONE.get(status, "queued")
            note = {"running": f"attempt {st.get('attempt', 0) + 1}", "queued": "next",
                    "needs-answers": "questions", "plan-review": "to approve", "blocked": "blocked",
                    "stopped": "stopped", "interrupted": "run ended"}.get(status, status)
        else:
            cls, note = "todo", ""
        em = f"<em>{esc(note)}</em>" if big and note else ""
        items.append(f"<li class='{cls}' title='{esc(s)}{': ' + esc(note) if note else ''}'><i></i>"
                     f"<span>{esc(s)}</span>{em}</li>")
    return f"<ol class='strip{' big' if big else ''}' aria-label='stages'>{''.join(items)}</ol>"


TABS = (("home", "/", "Home"), ("queue", "/runs/queue", "Queue"), ("sessions", "/runs", "Sessions"),
        ("vault", "/vault", "Vault"), ("settings", "/settings", "Settings"))


def nav(runs, active="", tabs=TABS):
    """The sticky bar's live part: tabs (Queue, with how many tickets wait on you)
    and the runner chip."""
    waiting = 0
    if runs and runs.queue_dir:
        for k in runs.ticket_keys():
            if runs.ticket_state(k).get("status") in WAITING + ("interrupted",):
                waiting += 1
    badge = (f" <span class='badge' title='{waiting} ticket{'s' * (waiting > 1)} waiting on you'>"
             f"{waiting}</span>") if waiting else ""
    links = []
    for name, href, label in tabs:
        if name in ("queue", "sessions") and not runs:
            continue
        on = " class='on' aria-current='page'" if active == name else ""
        links.append(f"<a href='{href}'{on}>{label}{badge if name == 'queue' else ''}</a>")
    chip = ""
    if runs and runs.queue_dir:
        r = runs.runner()
        if r["running"]:
            what = "between tickets"
            if r.get("ticket"):
                what = f"{esc(r['ticket'])} &middot; {esc(r.get('stage') or '')}"
                if r.get("step"):
                    what = f"<span title='{esc(r['step'])}'>{what}</span>"
            label = "halting" if r["halting"] else "running"
            chip = (f"<a class='runchip' href='/runs/queue' title='the queue run'><span class='sdot run'></span>"
                    f"<span class='rtxt'>{label} <b>{what}</b></span></a>")
        elif r["starting"]:
            chip = ("<a class='runchip' href='/runs/queue'><span class='sdot wait'></span>"
                    "<span class='rtxt'>queue starting&hellip;</span></a>")
        else:
            chip = ("<a class='runchip' href='/runs/queue'><span class='sdot'></span>"
                    "<span class='rtxt'>queue idle</span></a>")
    return f"<div class='tabs'>{''.join(links)}</div><div class='bar-right'>{chip}</div>"


def nav_bar(runs, active="", user=None, tabs=TABS):
    """The bar on every page: logo, live tabs and runner chip (#r-nav), how fresh the
    page is (#fresh, kept by the polling script) and who is signed in."""
    who = (f"<form method='post' action='/logout' class='bar-who'><span title='signed in as {esc(user)}'>{esc(user)}"
           "</span><button type='submit' class='ghost'>Log out</button></form>") if user else ""
    return ("<nav class='bar'><div class='bar-in'><a class='logo' href='/'>harness</a>"
            f"<div id='r-nav' class='bar-mid'>{nav(runs, active, tabs)}</div>"
            "<span id='fresh' class='fresh' role='status' aria-live='polite' hidden></span>"
            f"{who}</div></nav>")


def _live_tag(url, sig):
    return f"<span id='live' hidden data-live='{esc(url)}' data-sig='{esc(sig)}'></span>"


def _row(runs, row, live, extra="", now=None):
    sid = row["id"]
    title = row.get("title") or sid
    kids = row.get("kids") or 0
    meta = [esc(row["source"].label)]
    if row.get("agent"):
        meta.append("agent " + esc(row["agent"]))
    model = _model_name(row.get("model"))
    if model:
        meta.append(esc(model))
    if kids:
        meta.append(f"{kids} subagent session{'s' if kids != 1 else ''}")
    tok = _ms(row.get("tokens_input")) + _ms(row.get("tokens_output"))
    if tok:
        meta.append(f"{tok:,} tokens")
    meta.append(esc(row.get("directory") or ""))
    return (f"<li><span>{extra}<a href='/runs/{quote(sid)}'>{esc(title)}</a> {_status(runs, row, live, now)}</span>"
            f"<span class='when'>{_ago(_ms(row['time_updated']), now)}</span>"
            f"<span class='meta'>{' &middot; '.join(meta)}</span></li>")


def _stage_label(runs, key, row, st, outs):
    """(stage, attempt) of a ticket session: the stage output written after it
    started; none yet: the stage the ticket is running now."""
    name = runs.stage_of(key, _ms(row["time_created"]), outs)
    if name:
        stage, _, n = name.partition("-")
        return stage, int(n) + 1 if n.isdigit() else 0
    if st.get("status") in ("running", "interrupted"):
        return st.get("stage", ""), st.get("attempt", 0) + 1
    return "", 0


# --- /runs: every session ---------------------------------------------------------

def _list_body(runs, query=""):
    now = time.time()
    live = runs.live(now)
    rows = runs.sessions()
    q = query.lower()
    if q:
        rows = [r for r in rows if q in (r.get("title") or "").lower() or q in (r.get("directory") or "").lower()
                or q in r["id"].lower()]
    parts = []
    keys = runs.ticket_keys() if not q else []
    if keys:
        counts = {}
        for k in keys:
            s = runs.ticket_state(k).get("status", "")
            counts[s] = counts.get(s, 0) + 1
        bits = " ".join(f"{_pill(s)}<span class='muted'>&times;{n}</span>" for s, n in sorted(counts.items()))
        parts.append("<section class='panel'><div class='hd'><h2>Queue</h2>"
                     "<a class='btn sm' href='/runs/queue'>Open the queue &rarr;</a></div>"
                     f"<div class='counts'>{bits}</div>"
                     "<div class='muted'>A ticket waiting on you does not hold the queue: the run goes on to the "
                     "next ticket, and takes this one again once you answer, approve or retry it.</div></section>")
    live_rows = [r for r in rows if r["id"] in live]
    if live_rows:
        parts.append("<section class='panel'><h2>Live now</h2><ul class='runs'>")
        parts += [_row(runs, r, live, now=now) for r in live_rows]
        parts.append("</ul></section>")
    tickets, other = {}, []
    for r in rows:
        key = runs.ticket_of_row(r)
        if key:
            tickets.setdefault(key, []).append(r)
        else:
            other.append(r)
    if tickets:
        parts.append("<section class='panel'><h2>Queue tickets</h2>")
        for key in sorted(tickets, key=lambda k: -max(_ms(r["time_updated"]) for r in tickets[k])):
            outs = runs.stage_outs(key)
            st = runs.ticket_state(key)
            parts.append(f"<div class='ticket'><div class='ph'><a href='/runs/ticket/{quote(key)}'><b>{esc(key)}</b></a>"
                         f" {_pill(st.get('status', ''), st.get('stopping'))}</div><ul class='runs'>")
            for r in sorted(tickets[key], key=lambda r: _ms(r["time_created"])):
                stage, n = _stage_label(runs, key, r, st, outs)
                tag = f"<span class='tag'>{esc(stage or '?')}{f' &middot; {n}' if n else ''}</span> "
                parts.append(_row(runs, r, live, tag, now))
            parts.append("</ul></div>")
        parts.append("</section>")
    parts.append("<section class='panel'><h2>Sessions</h2>")
    if other:
        parts.append("<ul class='runs'>")
        parts += [_row(runs, r, live, now=now) for r in other]
        parts.append("</ul>")
    else:
        parts.append("<div class='muted'>No sessions" + (" match." if q else " yet.") + "</div>")
    parts.append("</section>")
    return "".join(parts), len(live)


def list_live(runs, query=""):
    body, n = _list_body(runs, query)
    regions = {"list": body, "nav": nav(runs, "sessions"), "count": f"{n} live"}
    return {"sig": _sig(regions), "regions": regions}


def render_list(runs, query=""):
    d = list_live(runs, query)
    url = "/runs/live" + (f"?q={quote(query)}" if query else "")
    return [_live_tag(url, d["sig"]),
            "<header class='ph'><h1>Sessions</h1>"
            f"<span class='pill t-run dot' id='r-count'>{d['regions']['count']}</span>"
            "<div class='sub'>Every opencode session on this host: container agents, host mode, opencode web "
            "and queue stages. <b>live</b> is a headless run you can message; <b>active</b> was updated in the "
            "last 90 s.</div></header>",
            "<form method='get' action='/runs' class='row'>"
            f"<input type='search' name='q' value='{esc(query)}' placeholder='filter by title, folder or id'>"
            "<button type='submit'>Filter</button></form>",
            f"<div id='r-list' class='stack'>{d['regions']['list']}</div>"]


# --- /runs/queue ------------------------------------------------------------------

def _hidden(name, value):
    return f"<input type='hidden' name='{esc(name)}' value='{esc(value)}'>"


def _btn(action, label, cls="", fields=(), confirm="", title="", disabled=False):
    conf = f" data-confirm='{esc(confirm)}'" if confirm else ""
    t = f" title='{esc(title)}' aria-label='{esc(title)}'" if title else ""
    return (f"<form method='post' action='{esc(action)}'{conf}>{''.join(_hidden(k, v) for k, v in fields)}"
            f"<button type='submit' class='{cls}'{t}{' disabled' if disabled else ''}>{label}</button></form>")


STOPPABLE = ("queued", "needs-answers", "plan-review", "blocked", "running", "interrupted")
RETRYABLE = ("blocked", "needs-answers", "stopped", "done", "interrupted")
RETRY_LABEL = {"blocked": "Retry", "needs-answers": "Plan again without answering", "stopped": "Resume",
               "done": "Run again", "interrupted": "Queue again"}
RETRY_TITLE = {"blocked": "queue it again at the stage it stopped at, as it is",
               "needs-answers": "queue it again: the planner plans again without your answers",
               "stopped": "queue it again where you stopped it",
               "done": "queue it again from the start",
               "interrupted": "queue it again at the stage the run was on"}
STOP_TITLE = {False: "take it out of the run's way: the run skips it until you resume it",
              True: "stop its agent now; it waits as stopped until you resume it"}
REMOVE_TITLE = "delete the ticket from the queue (its branch and the vault notes stay)"
STUCK_AFTER = 20   # s a stop or halt may take before the page offers to end the run


def _short(ref):
    """A commit id as its first 12, a ref name as it is."""
    return ref[:12] if re.fullmatch(r"[0-9a-f]{40}", ref or "") else ref
ANSWERABLE = ("needs-answers", "plan-review", "blocked", "queued", "stopped")


def _why(st):
    status = st.get("status", "")
    if st.get("stopping"):
        if _stuck(st):
            return ("still stopping: the run has not got to it, so it may be stuck; "
                    "End the run kills it and puts this ticket back")
        return "stopping: the run stops its agent within seconds"
    if status == "running":
        return f"{st.get('stage', '')}, attempt {st.get('attempt', 0) + 1}" + (f": {st['step']}" if st.get("step") else "")
    if status == "interrupted":
        return (f"the queue run ended while it was at {st.get('stage', '')}; "
                + ("the run going now takes it again there" if st.get("run_going")
                   else "start the queue to carry on there"))
    if status == "queued":
        return st.get("reason") or f"waits for the run; starts at {st.get('stage', '')}"
    if status == "done":
        said = [("" if p.get("repo", ".") in (".", "") else f"{p['repo']}: ") + f"pushed branch {p['branch']} to "
                f"{p['remote']}" + (f" ({p['sha'][:12]})" if p["sha"] else "")
                for p in st.get("pushes") or [st.get("push") or {}] if p.get("result") == "pushed"]
        if said:   # the reason says what was kept local or left uncommitted
            return "; ".join(said + [r for r in (st.get("reason") or "").split("; ") if r.startswith("changes left")])
        return st.get("reason") or (f"done at {st['sha'][:12]}" if st.get("sha") else "done")
    return st.get("reason") or ""


def _stuck(st):
    return st.get("stopping") and st.get("stop_wait", 0) > STUCK_AFTER


END_CONFIRM = ("End the queue run now? Its process and its agent are killed (any work the agent has not "
               "committed in this stage may be lost) and the ticket it is on goes back to the queue; a ticket "
               "you asked to stop stays stopped.")


def _end_btn(cls=""):
    return _btn("/runs/queue/end", "End the run", ("warn " + cls).strip(), confirm=END_CONFIRM,
                title="the run is not responding to stop or halt: kill it")


def _ticket_row(runs, key, st, i, n, now):
    status = st.get("status", "")
    base = f"/runs/ticket/{quote(key)}/"
    back = (("back", "queue"),)
    acts = [_btn(base + "move", "&uarr;", "sm ic", back + (("where", "up"),), title=f"Move {key} up",
                 disabled=i == 0),
            _btn(base + "move", "&darr;", "sm ic", back + (("where", "down"),), title=f"Move {key} down",
                 disabled=i == n - 1)]
    if status in ("needs-answers", "plan-review"):
        acts.append(f"<a class='btn sm primary' href='/runs/ticket/{quote(key)}' title="
                    + ("'open the ticket to answer the planner&#39;s questions'>Answer" if status == "needs-answers"
                       else "'open the ticket to read the plan and approve it or send feedback'>Review plan")
                    + "</a>")
    if status in STOPPABLE and not st.get("stopping"):
        acts.append(_btn(base + "stop", "Stop", "sm warn", back, title=STOP_TITLE[status == "running"],
                         confirm=f"Stop {key}?" + (" Its agent is stopped now." if status == "running" else "")))
    if _stuck(st):
        acts.append(_end_btn("sm"))
    if status in RETRYABLE:
        acts.append(_btn(base + "retry", RETRY_LABEL[status], "sm", back, title=RETRY_TITLE[status]))
    acts.append(_btn(base + "remove", "Remove", "sm danger", back, disabled=status == "running",
                     title="Stop it first" if status == "running" else REMOVE_TITLE,
                     confirm=f"Remove {key} from the queue? Its " + ("stage outputs are deleted (the project "
                             "and its branch stay)." if st.get("in_place") else "clone and stage outputs are deleted.")))
    tone = " wait" if status in WAITING + ("interrupted",) else " run" if status == "running" else ""
    proj = os.path.basename((st.get("project") or "").rstrip("/\\"))
    tags = f"<span class='tag' title='{esc(st.get('project', ''))}'>{esc(proj)}</span>" if proj else ""
    if st.get("start"):
        tags += (f" <span class='tag' title='the work starts from {esc(st['start'])}'>"
                 f"from {esc(_short(st['start']))}</span>")
    if st.get("branch") and st["branch"] != key:
        tags += f" <span class='tag' title='the branch the work ends on'>on {esc(st['branch'])}</span>"
    if st.get("in_place"):
        tags += " <span class='tag' title='its agents edit your project folder directly'>in your folder</span>"
    elif st.get("project"):
        tags += " <span class='tag' title='its agents work in a clone of the project; your folder is not touched'>in a copy</span>"
    if st.get("plan_review"):
        tags += " <span class='tag' title='the plan waits for your approval before implementing'>plan approval</span>"
    when = _iso_ms(st.get("updated", ""))
    return (f"<div class='tk{tone}'><span class='pos'>{i + 1}</span><div class='main'><div class='key'>"
            f"<a href='/runs/ticket/{quote(key)}'>{esc(key)}</a>{_pill(status, st.get('stopping'))}{tags}</div>"
            f"<div class='why' title='{esc(_why(st))}'>{esc(_why(st)) or '&nbsp;'}</div></div>"
            f"{stage_strip(st)}<span class='when' title='last changed'>{_ago(when, now) if when else ''}</span>"
            f"<div class='ra'>{''.join(acts)}</div></div>")


def _runner_card(runs, r, now, back=""):
    """The runner: what it is on, or the Start form (back: the ticket page it is on)."""
    parts = ["<section class='panel runner'>"]
    if r["running"]:
        mode = "host, single message" if r["single"] else "host" if r["host"] else "container"
        what = (f"<a href='/runs/ticket/{quote(r['ticket'])}'>{esc(r['ticket'])}</a> &middot; {esc(r.get('stage', ''))}"
                if r.get("ticket") and KEY_RE.match(r["ticket"]) else "between tickets")
        if r.get("ticket") and r.get("step"):
            what += f": {esc(r['step'])}"
        started = _iso_ms(r.get("started", ""))
        parts.append("<div class='state'><span class='sdot run'></span><h2>Queue running"
                     + (" <span class='pill t-wait dot'>halting</span>" if r["halting"] else "") + "</h2></div>")
        if not r["halting"]:
            parts.append("<div class='startf'>" + _btn("/runs/queue/halt", "Halt the run", "warn", confirm=(
                "Halt the queue run? The ticket it is on goes back to the queue at the same stage.")) + "</div>")
        else:
            parts.append("<div class='startf'><span class='muted'>the run ends at its next check; if it does not "
                         "within a minute it is stuck</span>" + _end_btn() + "</div>")
        parts.append(f"<div class='kv facts-row'><span>on <b>{what}</b></span><span>mode <b>{esc(mode)}</b></span>"
                     + (f"<span>model <b>{esc(r['model'])}</b></span>" if r.get("model") else "")
                     + (f"<span>since <b>{_ago(started, now)}</b></span>" if started else "")
                     + f"<span>heartbeat <b>{_ago(r['beat'] * 1000, now)}</b></span>"
                     + (f"<span>pid <b>{r['pid']}</b></span>" if r["pid"] else "") + "</div>")
    elif r["starting"]:
        parts.append("<div class='state'><span class='sdot wait'></span><h2>Queue starting&hellip;</h2></div>"
                     "<div class='startf muted'>checks the vault, the upstream login and the proxy</div>")
        out = runs.runner_out(6)
        if out:
            parts.append(f"<pre class='qlog facts-row'>{esc(out)}</pre>")
    else:
        waits = (f"{esc(back)} waits until a run takes it; tickets go one at a time, in the queue's order."
                 if back else "Tickets wait until a run takes them, one at a time, in this order.")
        parts.append("<div class='state'><span class='sdot'></span><div><h2>Queue not running</h2>"
                     f"<div class='muted'>{waits}</div></div></div>")
        parts.append("<form method='post' action='/runs/queue/start' class='startf' "
                     "data-confirm-host='Host mode runs the agents as you, with no container and no firewall. Start?'>"
                     + (_hidden("back", back) if back else "") +
                     "<div class='checks'><label><input type='radio' name='mode' value='container' checked> container</label>"
                     "<label><input type='radio' name='mode' value='host'> host</label>"
                     "<label><input type='radio' name='mode' value='single'> host, single message</label></div>"
                     "<label class='opt'><input type='checkbox' name='confirm_host' value='1'>"
                     "<span>host mode is OK<small>no container, no firewall</small></span></label>"
                     "<button type='submit' class='primary'>Start the queue</button></form>")
        if r["exited"]:
            rc, at = r["exited"]
            out = runs.runner_out(8)
            parts.append(f"<div class='facts-row'><div class='flash bad'>The run started here {_ago(at * 1000, now)} "
                         f"ended (status {rc}). <a href='/runs/queue/runner.out' target='_blank' rel='noopener'>"
                         f"its output</a></div>" + (f"<pre class='qlog'>{esc(out)}</pre>" if out else "") + "</div>")
    parts.append("</section>")
    return "".join(parts)


def queue_live(runs):
    now = time.time()
    keys = runs.ticket_keys()
    states = {k: runs.ticket_state(k) for k in keys}
    counts = {}
    for st in states.values():
        counts[st.get("status", "")] = counts.get(st.get("status", ""), 0) + 1
    order = ("running", "interrupted", "needs-answers", "plan-review", "blocked", "queued", "stopped", "done")
    summary = " ".join(f"{_pill(s)}<span class='muted'>{counts[s]}</span>" for s in order if counts.get(s))
    rows = [_ticket_row(runs, k, states[k], i, len(keys), now) for i, k in enumerate(keys)]
    tickets = (f"<div class='hd'><h2>Tickets <span class='muted'>{len(keys)}</span></h2>"
               f"<div class='counts'>{summary}</div></div>"
               + (f"<div class='tks'>{''.join(rows)}</div>" if rows else
                  "<div class='empty'>The queue is empty. Add tickets below."
                  f"<div class='muted'>Its folder: <code>{esc(runs.queue_dir or '(none given)')}</code></div></div>"))
    regions = {"runner": _runner_card(runs, runs.runner(now), now), "tickets": tickets, "nav": nav(runs, "queue")}
    return {"sig": _sig(regions), "regions": regions}


def _serve_project(runs):
    """The git repository harness serve was started in (its top folder), or ""."""
    d = runs.project_dir
    if not d or not os.path.isdir(d):
        return ""
    try:
        r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _projects(runs):
    """Project folders for the Add form, the default first: the repository
    harness serve was started in, then those tickets were added with, newest first."""
    here = _serve_project(runs)
    seen = [here] if here else []
    for k in sorted(runs.ticket_keys(), key=lambda k: -_iso_ms(runs.ticket_state(k).get("created", ""))):
        p = runs.ticket_state(k).get("project")
        if p and p not in seen:
            seen.append(p)
    return seen


AFTER_PLAN_CHOICES = (
    ("wait", "Wait for my approval", "default",
     "The ticket stops at \u201cplan to approve\u201d so you can read the plan. Approve it to start "
     "implementing, or send feedback: the planner revises the plan and it comes back to you. Nothing is "
     "built until you approve."),
    ("go", "Go straight on to implementing", "",
     "Implementing starts as soon as the plan is written. A plan with questions only you can answer still "
     "stops and waits for your answers."))


def _choice(legend, name, value, choices):
    """A fieldset of radio cards, each saying what it does; the first is the default."""
    value = value if value in [c[0] for c in choices] else choices[0][0]
    cards = "".join(f"<label><input type='radio' name='{name}' value='{c}'{' checked' if c == value else ''}>"
                    f"{esc(lab)}{f' <span class=muted>({esc(tag)})</span>' if tag else ''}<small>{esc(hint)}</small>"
                    "</label>" for c, lab, tag, hint in choices)
    return (f"<fieldset id='f-{name}'><legend>{esc(legend)}</legend><div class='presets two'>{cards}</div>"
            "<div class='why' hidden></div></fieldset>")


START_MODES = ("new", "continue", "here")


def _start_choice(v, sent):
    """The Starting point cards; Continue carries the branch to continue."""
    mode = v("start") if v("start") in START_MODES else ("continue" if sent and v("ref") and v("ref") != "here"
                                                          else "here" if sent and v("ref") == "here" else "new")
    cont = v("cont") or (v("ref") if mode == "continue" and not v("start") else "")
    cards = (
        ("new", "New work", "a fresh branch from the target branch below; the folder's uncommitted files "
         "come along."),
        ("continue", "Continue existing work", "pick up a branch that already has work on it (a remote one is "
         "fetched first); the work goes on on that branch."),
        ("here", "The folder as it is now", "start from the commit the project folder has checked out, "
         "whatever branch that is."))
    out = []
    for c, lab, hint in cards:
        extra = ""
        if c == "continue":
            extra = (f"<input name='cont' list='branches' spellcheck='false' value='{esc(cont)}' "
                     "placeholder='origin/ABC-123' aria-label='branch or commit to continue'>")
        out.append(f"<label><input type='radio' name='start' value='{c}'{' checked' if c == mode else ''}>"
                   f"{esc(lab)}<small>{esc(hint)}</small><small class='why' hidden></small>{extra}</label>")
    return (f"<fieldset id='f-start'><legend>Starting point</legend><div class='presets three'>{''.join(out)}</div>"
            "<div class='why' hidden></div></fieldset>")


def add_form(runs, form=None, err=""):
    """The Add tickets form; after a refused add, the values sent (form) and
    why it was refused (err), so nothing typed is lost."""
    projects = _projects(runs)
    v = (lambda name: _one(form, name)) if form is not None else (lambda name: "")
    preset = v("preset") if v("preset") in (*PRESETS, "custom") else "full"
    on = PRESETS[preset] if preset in PRESETS else [s for s in STAGES if v("stage_" + s)]
    presets = "".join(f"<label><input type='radio' name='preset' value='{p}'{' checked' if p == preset else ''}>"
                      f"{esc(lab)}<small>{esc(hint)}</small></label>" for p, lab, hint in PRESET_LABELS)
    presets += (f"<label><input type='radio' name='preset' value='custom'{' checked' if preset == 'custom' else ''}>"
                "Custom<small>pick the stages below</small></label>")
    checks = "".join(f"<label><input type='checkbox' name='stage_{s}' value='{s}'{' checked' if s in on else ''}> "
                     f"{s}</label>" for s in STAGES)
    opts = "".join(f"<option value='{esc(p)}'>" for p in projects)
    project = v("project") if form is not None else (projects[0] if projects else "")
    sent = {k: _one(form, k) for k in form if k.startswith("repo_")} if form is not None else {}
    return (f"<section class='panel' id='add'{' data-err=1' if err else ''}><div class='hd'><h2>Add tickets</h2></div>"
            + (f"<p class='err' role='alert'>Not added: {esc(err)}</p>" if err else "") +
            "<form method='post' action='/runs/queue/add' id='addform' class='stack'>"
            "<div class='grid2'>"
            "<label class='fld'>Ticket keys<input name='keys' required autocomplete='off' spellcheck='false' "
            f"value='{esc(v('keys'))}' placeholder='ABC-123 ABC-124'>"
            "<small>one or more, space or comma separated. Each is its own ticket, all with the settings below; "
            "a work branch it gets is named after its key</small></label>"
            f"<label class='fld'>Project folder<input name='project' required list='projects' spellcheck='false' "
            f"value='{esc(project)}' placeholder='/home/me/src/project'>"
            f"<datalist id='projects'>{opts}</datalist><datalist id='branches'></datalist>"
            "<small>the git repository the work is in (absolute path)</small></label>"
            "</div>"
            "<div class='projhint muted' id='projhint' aria-live='polite'></div>"
            "<fieldset id='f-repos' hidden><legend>Repositories</legend>"
            "<small class='muted'>The agents work in all of them. For each, the ticket's changes are left "
            "uncommitted or made on a branch.</small>"
            f"<div class='repotree' id='repotree' data-sent='{esc(json.dumps(sent))}'></div>"
            "<div class='why' hidden></div></fieldset>"
            f"<fieldset><legend>What still needs doing</legend><div class='presets'>{presets}</div>"
            f"<div class='checks' id='stagechecks'>{checks}</div><div class='why' hidden></div></fieldset>"
            + _start_choice(v, form is not None) +
            "<div class='grid2' id='f-branch'>"
            f"<label class='fld'>Target branch<input name='base' list='branches' spellcheck='false' "
            f"value='{esc(v('base'))}' placeholder='the branch the folder is on'>"
            "<small>the branch this work is meant to go into: review compares the work with it, and New work "
            "starts from it</small></label>"
            "<details class='fld adv' data-k='addadv'" + (" open" if v("branch") else "") + ">"
            "<summary>Work branch name</summary>"
            f"<label class='fld'><input name='branch' spellcheck='false' value='{esc(v('branch'))}' "
            "placeholder='the ticket key'><small>the branch the agents commit to. Empty: the ticket key, or the "
            "branch you continue. An existing branch is only moved forward, never rewritten.</small>"
            "<small class='why' id='branchwhy' hidden></small></label>"
            "</details>"
            "</div>"
            + _choice("When the plan is written", "after_plan", v("after_plan"), AFTER_PLAN_CHOICES) +
            "<label class='fld'>Note for the agents<textarea name='note' maxlength='4000' "
            "placeholder='Context every stage gets, e.g. the solution is on origin/ABC-3122; only the "
            f"review is left.'>{esc(v('note'))}</textarea></label>"
            "<div class='formacts'><button type='submit' class='primary'>Add to the queue</button>"
            "<div class='muted' id='addhint' aria-live='polite'></div></div>"
            "</form></section>")


def render_queue_page(runs, msg="", bad=False, form=None, err=""):
    d = queue_live(runs)
    return [_live_tag("/runs/queue/live", d["sig"]),
            "<header class='ph'><h1>Queue</h1><a class='btn sm' href='#add'>+ Add tickets</a>"
            "<div class='sub'>The run works the tickets top down, one stage at a time. A ticket waiting on you "
            "(questions, a plan to approve, blocked) does not hold the queue.</div></header>",
            _flash(msg, bad),
            f"<div id='r-runner'>{d['regions']['runner']}</div>",
            f"<section class='panel flush' id='r-tickets'>{d['regions']['tickets']}</section>",
            add_form(runs, form, err)]


# --- /runs/ticket/KEY ---------------------------------------------------------------

def _note_form(key, button):
    """A blocked ticket: one note for the agents, sent the same way as answers."""
    return (f"<form method='post' action='/runs/ticket/{quote(key)}/answer' class='qs'>"
            "<label><div class='q'>What should change</div><textarea name='note' required placeholder='For "
            "example: the tests need the database running first, or: keep the old API and add the new one beside "
            "it'></textarea></label>"
            f"<div class='acts'><button type='submit' class='primary'>{button}</button></div></form>")


def _answer_form(key, qs, review=False):
    parts = [f"<form method='post' action='/runs/ticket/{quote(key)}/answer' class='qs'>"]
    for i, q in enumerate(qs[:FORM_QUESTIONS]):
        cls = "q blocking" if q.get("blocking") else "q"
        parts.append(f"<label><div class='{cls}'>{esc(q['text'])}</div>"
                     f"<input type='hidden' name='q{i}' value='{esc(q['text'])}'>"
                     f"<textarea name='a{i}' placeholder='Your answer (leave empty to go with its default)'>"
                     "</textarea></label>")
    extra = "Anything else for the planner" if qs else "Your answers, or anything the planner should know"
    if review:
        extra = "Your feedback on the plan: what to change, add or drop"
    elif len(qs) > FORM_QUESTIONS:
        extra = "Answers to the remaining questions, and anything else"
    go = "" if review else ("<button type='submit' name='defaults' value='1' title='The plan is made again with "
                            "the defaults and the ticket goes on to implement without coming back to you'>"
                            "Go with the defaults</button>")
    parts.append(f"<label><div class='q'>{extra}</div><textarea name='note'></textarea></label>"
                 "<div class='acts'><button type='submit' class='primary'>"
                 f"{'Send feedback and plan again' if review else 'Send answers and plan again'}</button>{go}</div></form>")
    return "".join(parts)


def _t_head(runs, key, st, pos, n):
    status = st.get("status", "")
    facts = []
    if st.get("project"):
        facts.append(f"<span>project <b>{esc(st['project'])}</b></span>")
    if st.get("base"):
        facts.append(f"<span>base <b>{esc(st['base'])}</b></span>")
    if st.get("start"):
        facts.append(f"<span>starts from <b title='{esc(st['start'])}'>{esc(_short(st['start']))}</b></span>")
    facts.append(f"<span>branch <b>{esc(st.get('branch') or key)}</b></span>")
    facts.append("<span>works <b>" + ("in your project folder" if st.get("in_place") else "in a separate copy")
                 + "</b></span>")
    facts.append("<span>after the plan <b>" + ("waits for your approval" if st.get("plan_review")
                                               else "goes straight on") + "</b></span>")
    if st.get("sha"):
        facts.append(f"<span>commit <b>{esc(st['sha'][:12])}</b></span>")
    if st.get("created"):
        facts.append(f"<span>added <b>{esc(st['created'][:16].replace('T', ' '))}</b></span>")
    upd = _iso_ms(st.get("updated", ""))
    if upd:
        facts.append(f"<span>updated <b>{_ago(upd)}</b></span>")
    where = f"<span class='muted'>{pos} of {n} in the queue</span>" if pos else ""
    return ("<header class='ph'><div class='crumbs'><a href='/runs/queue'>Queue</a><span>/</span>"
            f"<b>{esc(key)}</b></div><h1>{esc(key)}</h1>{_pill(status, st.get('stopping'))}{where}"
            f"<a class='btn' href='/runs/ticket/{quote(key)}/diff'>Changes</a>"
            f"<div class='sub'>{esc(_why(st))}</div></header>"
            f"<section class='panel'>{stage_strip(st, True)}<div class='kv'>{''.join(facts)}</div></section>" + _t_result(st))


PUSHED = {"pushed": "pushed to {remote}", "local": "kept on this computer: not pushed",
          "rejected": "the push to {remote} was refused", "skipped": "not pushed: the ticket has no push stage"}


def _t_result(st):
    """What the ticket ended with (the queue writes it at push, or when its stages
    end without one): where the branch is, the pull request link, its commits."""
    p = st.get("push")
    if not p:
        return ""
    stat = st["diffstat"]
    facts = []
    for x in st.get("pushes") or ([p] if p.get("branch") else []):   # a branch per repository; older states: one
        where = "" if x.get("repo", ".") in (".", "") else f"{esc(x['repo'])}: "
        facts.append(f"<span>{where}branch <b>{esc(x['branch'])}</b>" +
                     (f" at <b>{esc(x['sha'][:12])}</b>" if x["sha"] else "") + "</span>")
        facts.append(f"<span>{where}<b>{esc(PUSHED[x['result']].format(remote=x['remote'] or 'the remote'))}</b>"
                     "</span>")
    left = st.get("left_uncommitted") or []
    if left:
        facts.append("<span>left uncommitted in <b>" + esc(", ".join("the project" if r == "." else r for r in left))
                     + "</b></span>")
    facts.append(f"<span>changed <b>{stat['files']} file(s), +{stat['insertions']} &minus;{stat['deletions']}</b></span>")
    if p["url"] and p.get("branch"):
        facts.append(f"<span>remote <b>{esc(p['url'])}</b></span>")
    if st.get("folder_branch_now"):
        facts.append(f"<span>your folder is on <b>{esc(st['folder_branch_now'])}</b></span>")
    link = (f"<div class='acts'><a class='btn primary' href='{esc(st['compare_url'])}' target='_blank' "
            "rel='noopener'>Open a pull request &rarr;</a></div>") if st.get("compare_url") else ""
    bad = f"<div class='flash bad'>{esc(p['error'])}</div>" if p["error"] else ""
    warn = "".join(f"<div class='flash bad'>{esc(w)}</div>" for w in st.get("warnings") or [])
    commits = "".join(f"<li><code>{esc(c['sha'][:12])}</code> {esc(c['subject'])}</li>" for c in st["commits"])
    commits = (f"<details class='doc' data-k='commits'><summary>Commits<span class='muted'>{len(st['commits'])}"
               f"</span></summary><ul class='commits'>{commits}</ul></details>") if commits else \
        ("" if left and not st.get("pushes") else "<div class='muted'>No commits on the branch.</div>")
    return (f"<section class='panel'><h2>Result</h2><div class='kv'>{''.join(facts)}</div>{bad}{warn}"
            f"{link}{commits}</section>")


def _t_acts(runs, key, st, rows, live, pos, n):
    status = st.get("status", "")
    base = f"/runs/ticket/{quote(key)}/"
    parts = []
    now_live = [r for r in rows if r["id"] in live]
    if now_live:
        r = max(now_live, key=lambda r: _ms(r["time_created"]))
        parts.append(f"<a class='btn primary' href='/runs/{quote(r['id'])}'>Watch and message the agent &rarr;</a>")
    if status == "plan-review":
        parts.append(_btn(base + "approve", "Approve the plan", "primary",
                          title="implementing starts when the queue run gets to this ticket"))
    if _stuck(st):
        parts.append(_end_btn())
    if status in RETRYABLE:
        parts.append(_t_retry(runs, key, st))
    if status in STOPPABLE and not st.get("stopping"):
        parts.append(_btn(base + "stop", "Stop", "warn", title=STOP_TITLE[status == "running"], confirm=f"Stop {key}?" + (
            " Its agent is stopped now; Resume takes it again." if status == "running" else "")))
    if pos:
        parts.append("<span class='sep'></span>")
        for where, label in (("top", "Top"), ("up", "Up"), ("down", "Down"), ("bottom", "Bottom")):
            dis = (pos == 1 and where in ("top", "up")) or (pos == n and where in ("down", "bottom"))
            parts.append(_btn(base + "move", label, "sm", (("where", where),), disabled=dis,
                              title=f"Move {key} {where}"))
    parts.append("<span class='sep'></span>")
    parts.append(_btn(base + "remove", "Remove", "sm danger", disabled=status == "running",
                      title="Stop it first: its agent is working on it" if status == "running" else REMOVE_TITLE,
                      confirm=f"Remove {key}? Its " + ("outputs and log are deleted (the project and its "
                              "branch stay)." if st.get("in_place") else "clone, outputs and log are deleted.")))
    return f"<div class='acts'>{''.join(parts)}</div>"


def _t_retry(runs, key, st):
    """The retry form: from where it was (or the start) and each stage the queue
    would take it at (its own retry_refused); why not, when it takes none."""
    status = st.get("status", "")
    label = RETRY_LABEL[status]
    why = runs.retry_choices(key) or dict.fromkeys((None,) + STAGES, "")   # unknown: all, the queue still says no
    stages = [s for s in STAGES if not why.get(s)]
    if why.get(None) and not stages:
        return f"<span class='muted'>{esc(label)}: {esc(why[None])}</span>"
    opts = "".join(f"<option value='{s}'{' selected' if s == st.get('stage') else ''}>{s}</option>" for s in stages)
    if not why.get(None):
        opts = f"<option value=''>{'where it was' if status != 'done' else 'the start'}</option>" + opts
    pick = f"<label class='muted'>from <select name='stage' aria-label='stage'>{opts}</select></label>" if stages else ""
    note = f"<span class='muted'>not {'from where it was' if status != 'done' else 'from the start'}: " \
           f"{esc(why[None])}</span>" if why.get(None) else ""
    return (f"<form method='post' action='/runs/ticket/{quote(key)}/retry'>"
            f"<button type='submit' title='{esc(RETRY_TITLE[status])}'>{label}</button>"
            f"{pick}</form>{note}")


def _t_start(runs, key, st, r, now):
    """The queue's Start form (the queue page's own), on a ticket a run would take
    when none is going: an approve, answer or retry never starts one by itself."""
    if st.get("status") not in ("queued", "interrupted") or r["running"]:
        return ""
    return _runner_card(runs, r, now, back=key)


def _t_questions(runs, key, st):
    status = st.get("status", "")
    qs = st.get("questions") or []
    vault = runs.vault_of(st)
    if status == "blocked" and vault:
        # Blocked at any stage, not by the planner: retry as is, or send a note
        stage = st.get("stage", "")
        planned = "plan" in (st.get("stages") or STAGES)
        then = ("the ticket goes back to planning with it, and the revised plan comes back here for your "
                "approval before anything is built." if planned else f"the ticket runs {stage} again with it.")
        intro = (f"It stopped at {esc(stage)}" + (f": {esc(st['reason'])}" if st.get("reason") else "") + ". "
                 f"If you fixed the cause yourself (a setting, the run mode, something on your machine), use "
                 f"Retry above: it runs {esc(stage)} again as it is (add a note under Notes first and every "
                 f"stage gets it). If the work itself should change, write it here: {then}")
        return (f"<section class='panel'><h2>Get it going again</h2><div class='muted'>{intro}</div>"
                f"{_note_form(key, 'Send and plan again' if planned else f'Send and run {esc(stage)} again')}"
                "</section>")
    if status in WAITING and vault:
        review = status == "plan-review"
        head = "Approve the plan, or send feedback" if review else "Questions for you" if qs else "Answer the planner"
        again = (" The planner revises the plan with them and it comes back here: nothing is built until "
                 "you approve it.")
        intro = ("Approve the plan to start building, or answer and give feedback here." + again if review
                 else "The plan is waiting on the blocking questions. Answer what you can; an empty box takes "
                 "the question's default." + again + " Go with the defaults instead: the plan is made "
                 "with every default and implementing starts without it coming back to you.")
        plan = runs.note(key, "plan", vault)
        if plan.strip():   # what is to be approved or answered, where it is decided
            plan = (f"<details class='doc' data-k='qplan' open><summary>The plan<span class='muted'>"
                    f"{len(plan.splitlines())} lines</span></summary><pre class='qlog'>{esc(_cap(plan)[0])}</pre>"
                    "</details>")
        return (f"<section class='panel'><h2>{head}</h2><div class='muted'>{intro}</div>"
                f"{plan}{_answer_form(key, qs, review)}</section>")
    if qs:
        items = "".join(f"<li><div class='{'q blocking' if q.get('blocking') else 'q'}'>{esc(q['text'])}</div>"
                        f"<div class='muted'>{'waiting on you' if q.get('blocking') else 'going with the default'}"
                        "</div></li>" for q in qs)
        return f"<section class='panel'><h2>Questions</h2><ul class='qs'>{items}</ul></section>"
    return ""


def _t_sessions(runs, key, st, rows, live, outs, now):
    parts = [f"<h2>Sessions <span class='muted'>{len(rows)}</span></h2>"]
    if not rows:
        parts.append("<div class='muted'>No session for this ticket in the opencode databases yet.</div>")
        return "".join(parts)
    for r in reversed(rows):
        stage, n = _stage_label(runs, key, r, st, outs)
        created, updated = _ms(r["time_created"]), _ms(r["time_updated"])
        meta = [f"<span>{esc(r['source'].label)}</span>"]
        model = _model_name(r.get("model"))
        if model:
            meta.append(f"<span>{esc(model)}</span>")
        meta.append(f"<span>started {esc(_when(created))}</span>")
        if updated > created:
            meta.append(f"<span>{'ran' if r['id'] not in live else 'running'} {_dur(updated - created)}</span>")
        if r.get("kids"):
            meta.append(f"<span>{r['kids']} subagent{'s' if r['kids'] != 1 else ''}</span>")
        tok = _ms(r.get("tokens_input")) + _ms(r.get("tokens_output"))
        if tok:
            meta.append(f"<span>{tok:,} tokens</span>")
        tag = f"<span class='tag'>{esc(stage or '?')}{f' &middot; {n}' if n else ''}</span>"
        parts.append(f"<div class='ses'><div class='t'>{tag}<a href='/runs/{quote(r['id'])}'>"
                     f"{esc(r.get('title') or r['id'])}</a>{_status(runs, r, live, now)}</div>"
                     f"<span class='when'>{_ago(updated, now)}</span><div class='meta'>{''.join(meta)}</div></div>")
    return "".join(parts)


def _t_attempts(key, st, names):
    hist = st.get("history") or []
    parts = [f"<h2>Attempts <span class='muted'>{len(hist)}</span></h2>"]
    if not hist:
        parts.append("<div class='muted'>No attempt has finished yet. Older tickets keep theirs in the queue log.</div>")
        return "".join(parts)
    parts.append("<ul class='attempts'>")
    linked = set()   # a retried stage writes <stage>-N.out again: link only its newest attempt
    tone = {"passed": "t-ok", "failed": "t-bad", "blocked": "t-bad", "stopped": "t-off", "halted": "t-off"}
    for h in reversed(hist):
        stage, n, res = h["stage"], h["attempt"], h["result"]
        out = f"{stage}-{n - 1}.out"
        link = ""
        if out in names and out not in linked:
            linked.add(out)
            link = f"<a href='/runs/ticket/{quote(key)}/{quote(out)}' target='_blank' rel='noopener'>output</a>"
        parts.append(f"<li><div class='hd'><span class='pill {tone.get(res, 't-off')}'>{esc(res)}</span>"
                     f"<span class='tag'>{esc(stage)}</span><span>attempt {n}</span>"
                     f"<span class='muted'>{esc(h['at'][:16].replace('T', ' '))}</span>{link}</div>")
        if h["details"]:
            parts.append("<ul>" + "".join(f"<li>{esc(x)}</li>" for x in h["details"]) + "</ul>")
        parts.append("</li>")
    parts.append("</ul>")
    return "".join(parts)


def _t_notes(key, st):
    notes = st.get("notes") or []
    parts = [f"<h2>Notes <span class='muted'>{len(notes)}</span></h2>"]
    if notes:
        parts.append("<ul class='notes'>" + "".join(
            f"<li><span class='at'>{esc(n['at'][:16].replace('T', ' '))}</span><div class='txt'>{esc(n['text'])}</div></li>"
            for n in reversed(notes)) + "</ul>")
    else:
        parts.append("<div class='muted'>Context for the agents: every stage from now on gets your notes.</div>")
    parts.append(f"<form method='post' action='/runs/ticket/{quote(key)}/note' class='send'>"
                 "<textarea name='text' required maxlength='4000' placeholder='e.g. the fix is on origin/ABC-3122; "
                 "keep the public API as it is'></textarea><button type='submit'>Add note</button></form>")
    return "".join(parts)


def _t_outs(key, outs, now):
    parts = ["<h2>Stage outputs</h2>"]
    if not outs:
        return parts[0] + "<div class='muted'>None yet.</div>"
    parts.append("<ul class='runs'>")
    for name, mtime, size in reversed(outs):
        parts.append(f"<li><span><a href='/runs/ticket/{quote(key)}/{quote(name)}' target='_blank' "
                     f"rel='noopener'>{esc(name)}</a></span><span class='when'>{_ago(mtime * 1000, now)}</span>"
                     f"<span class='meta'>{size:,} bytes</span></li>")
    parts.append("</ul>")
    return "".join(parts)


def _t_docs(runs, key, st, log):
    vault = runs.vault_of(st)
    parts = []
    for kind, label in (("plan", "Plan"), ("answers", "Your answers"), ("review", "Review"), ("findings", "Findings")):
        if kind == "plan" and st.get("status") in WAITING and st.get("status") != "blocked":
            continue   # shown open above, with the answer form
        text = runs.note(key, kind, vault)
        if text.strip():
            parts.append(f"<section class='panel'><details class='doc' data-k='{kind}'><summary>{label}"
                         f"<span class='muted'>{len(text.splitlines())} lines</span></summary>"
                         f"<pre class='qlog'>{esc(_cap(text)[0])}</pre></details></section>")
    text = log[-60000:] if log.strip() else "no log yet"
    parts.append("<section class='panel'><details class='doc' data-k='log'><summary>Queue log"
                 f"<span class='muted'>{len(log.splitlines())} lines</span></summary>"
                 f"<pre class='qlog'>{esc(text)}</pre></details></section>")
    return "".join(parts)


def ticket_live(runs, key):
    """The ticket page's regions, or None when the ticket is gone."""
    if not KEY_RE.match(key or "") or key not in runs._ticket_dirs():
        return None
    try:
        with open(os.path.join(runs.queue_dir, key, "log.md"), encoding="utf-8", errors="replace") as f:
            log = f.read()
    except OSError:
        log = ""
    now = time.time()
    live = runs.live(now)
    outs = runs.stage_outs(key)
    st = runs.ticket_state(key)
    rows = runs.ticket_sessions(key)
    order = runs.ticket_keys()
    try:
        with open(os.path.join(runs.queue_dir, "queue.list"), encoding="utf-8") as f:
            listed = f.read().split()
    except OSError:
        listed = []
    pos = listed.index(key) + 1 if key in listed else 0
    n = len([k for k in listed if k in order])
    regions = {"head": _t_head(runs, key, st, pos, n), "acts": _t_acts(runs, key, st, rows, live, pos, n),
               "start": _t_start(runs, key, st, runs.runner(now), now), "qs": _t_questions(runs, key, st),
               "sessions": _t_sessions(runs, key, st, rows, live, outs, now),
               "attempts": _t_attempts(key, st, {o[0] for o in outs}), "notes": _t_notes(key, st),
               "outs": _t_outs(key, outs, now), "docs": _t_docs(runs, key, st, log), "nav": nav(runs, "queue")}
    return {"sig": _sig(regions), "regions": regions}


def render_ticket(runs, key, msg="", bad=False):
    d = ticket_live(runs, key)
    if d is None:
        return None
    g = d["regions"]
    return key, [_live_tag(f"/runs/ticket/{quote(key)}/live", d["sig"]),
                 f"<div id='r-head' class='stack'>{g['head']}</div>", _flash(msg, bad),
                 f"<section class='panel' id='r-acts'>{g['acts']}</section>",
                 f"<div id='r-start'>{g['start']}</div>", f"<div id='r-qs'>{g['qs']}</div>",
                 "<div class='wide'><div class='stack'>"
                 f"<section class='panel' id='r-sessions'>{g['sessions']}</section>"
                 f"<section class='panel' id='r-attempts'>{g['attempts']}</section>"
                 f"<div id='r-docs' class='stack'>{g['docs']}</div></div>"
                 f"<div class='stack'><section class='panel' id='r-notes'>{g['notes']}</section>"
                 f"<section class='panel' id='r-outs'>{g['outs']}</section></div></div>"]


# --- actions ------------------------------------------------------------------------

TICKET_ACTIONS = ("answer", "approve", "retry", "stop", "remove", "move", "note")
QUEUE_ACTIONS = ("add", "start", "halt", "end")


def _one(form, name):
    v = form.get(name) or ""
    return (v[0] if isinstance(v, list) and v else v if isinstance(v, str) else "")


def answer_text(form):
    """The answers note from the ticket page's form, or "" when nothing was answered."""
    def one(name):
        return _one(form, name).strip()
    out = []
    for i in range(FORM_QUESTIONS):
        q, a = one(f"q{i}"), one(f"a{i}")
        if a:
            out.append(f"## {q or 'Question ' + str(i + 1)}\n\n{a}")
    if one("note"):
        out.append(f"## Other\n\n{one('note')}")
    if not out and not one("defaults"):
        return ""
    out.append("Any question not answered here: go with its default.")
    return "\n\n".join(out)


def ref_ok(ref):
    """A branch, tag, remote-tracking ref or commit: nothing an option, a range or
    revision syntax could hide in."""
    return bool(REF_RE.match(ref)) and ".." not in ref and "//" not in ref and not ref.endswith(("/", ".lock", "."))


def _queue(runs, args, text=None):
    """Run the queue script (argv, never a shell); (ok, its last line)."""
    cmd = [sys.executable, QUEUE_SCRIPT, "--state", runs.queue_dir] + args
    try:
        p = subprocess.run(cmd, input=(text or "").encode("utf-8"), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=120, **NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"the queue did not answer: {e}"
    out = p.stdout.decode("utf-8", "replace")
    lines = [x for x in out.splitlines() if x.strip()]
    if p.returncode and ("invalid choice" in out or "unrecognized arguments" in out):
        return False, (f"this harness's queue does not have that command yet ({args[0]}); "
                       "update harness: " + (lines[-1] if lines else ""))
    if p.returncode:
        bad = [x.replace("[queue] ", "", 1) for x in lines if "[queue]" in x] or lines
        return False, "; ".join(bad[-3:]) or f"the queue exited with status {p.returncode}"
    return True, "; ".join(x.replace("[queue] ", "", 1) for x in lines[-3:])


def _waits(runs, key, res):
    """An answer, approve or retry that queued the ticket with no run going or
    starting: say so (the ticket page shows Start the queue; nothing starts by itself)."""
    ok, msg = res
    r = runs.runner()
    if ok and runs.ticket_state(key).get("status") == "queued" and not (r["running"] or r["starting"]):
        msg += "; the queue is not running, so nothing happens until you start it (Start the queue, below)"
    return ok, msg


def ticket_action(runs, key, action, form):
    """A ticket's button, through the queue script as harness queue does it.
    Returns (ok, message)."""
    if not KEY_RE.match(key or "") or key not in runs._ticket_dirs() or action not in TICKET_ACTIONS:
        return False, "no such ticket or action"
    st = runs.ticket_state(key)
    status = st.get("status") or "gone"
    again = "; reload its page"
    if action == "answer":
        vault = runs.vault_of(st)
        if not vault:
            return False, "no vault is set, so there is nowhere to keep the answers"
        if status not in ANSWERABLE:   # sent twice, or answered elsewhere since the page was loaded
            return False, f"{key} is {status} now, not waiting on answers{again}"
        text = answer_text(form)
        if not text:
            return False, "nothing to send: answer a question, or choose Go with the defaults"
        go = ["--go"] if _one(form, "defaults").strip() else []   # took the defaults: no plan review
        return _waits(runs, key, _queue(runs, ["answer", key, "--vault", vault, "--stdin"] + go, text))
    if action == "approve":
        if status != "plan-review":
            return False, f"{key} is {status}, not a plan waiting for approval{again}"
        return _waits(runs, key, _queue(runs, ["approve", key]))
    if action == "retry":
        stage = _one(form, "stage")
        if stage and stage not in STAGES:
            return False, "not a stage: " + stage[:40]
        if status not in RETRYABLE:
            return False, (f"{key} is {status}; only a blocked, stopped, done, waiting or interrupted ticket "
                           f"is retried{again}")
        # --harness: an agent a run that is gone left behind is ended, its container too
        return _waits(runs, key, _queue(runs, ["retry", key] + (["--stage", stage] if stage else [])
                                        + ["--harness", runs.harness]))
    if action == "stop":
        if status not in STOPPABLE:
            return False, f"{key} is {status}; there is nothing to stop"
        return _queue(runs, ["stop", key, "--harness", runs.harness])
    if action == "remove":
        if status == "running":   # its agent is working in the clone the remove deletes
            return False, f"{key} is running; stop it first{again}"
        return _queue(runs, ["remove", key, "--harness", runs.harness])
    if action == "move":
        where = _one(form, "where")
        if where not in MOVES:
            return False, "move takes up, down, top or bottom"
        return _queue(runs, ["move", key, where])
    text = _one(form, "text").replace("\r\n", "\n")   # note
    if not text.strip():
        return False, "the note is empty"
    if len(text) > NOTE_MAX:
        return False, f"the note is over {NOTE_MAX} characters"
    return _queue(runs, ["note", key, "--stdin"], text)


def project_path(p, nt=os.name == "nt"):
    """A project folder as it gets pasted: no surrounding blanks or quotes (Windows'
    Copy as path quotes it), ~ expanded, and on Windows a Git Bash /c/... path as
    C:/..., which Windows Python does not resolve."""
    p = p.strip()
    if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1].strip()
    p = os.path.expanduser(p)
    m = re.match(r"/([A-Za-z])(/.*)?$", p) if nt else None
    return f"{m.group(1).upper()}:{m.group(2) or '/'}" if m else p


_BRANCHES = {}


def project_branches(project):
    """The Add form's branch suggestions for a project folder: the repository's top
    folder, the branch it is on, origin's default branch and the local and
    remote-tracking branches, newest commit first. Read-only git; cached 5 s."""
    p = project_path(project)
    if not p or not os.path.isabs(p) or not os.path.isdir(p):
        return {"error": "not a folder (give an absolute path)"}
    now = time.time()
    hit = _BRANCHES.get(p)
    if hit and now - hit[0] < 5:
        return hit[1]
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")

    def git(*a):
        try:
            r = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", p, *a], capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL, env=env, **NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    top = git("rev-parse", "--show-toplevel")
    if top is None:
        out = {"error": "not in a git repository"}
    else:
        local, remote = [], []
        for line in (git("for-each-ref", "--sort=-committerdate", "--count=300", "--format=%(refname)",
                         "refs/heads", "refs/remotes") or "").splitlines():
            if line.startswith("refs/heads/"):
                local.append(line[len("refs/heads/"):])
            elif line.startswith("refs/remotes/") and not line.endswith("/HEAD"):
                remote.append(line[len("refs/remotes/"):])
        remotes = (git("remote") or "").split()
        defaults = []
        for r in remotes:
            d = git("symbolic-ref", "-q", "--short", f"refs/remotes/{r}/HEAD") or ""
            if d.startswith(r + "/") and d[len(r) + 1:] not in defaults:
                defaults.append(d[len(r) + 1:])
        out = {"top": top, "current": git("symbolic-ref", "-q", "--short", "HEAD") or "",
               "default": git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD") or "",
               "local": local, "remote": remote, "remotes": remotes, "defaults": defaults}
    _BRANCHES[p] = (now, out)
    if len(_BRANCHES) > 50:
        _BRANCHES.pop(next(iter(_BRANCHES)))
    return out


def _branch_of_ref(info, ref):
    """The branch name continuing ref ends on: ref itself when it is a local branch,
    NAME for a remote-tracking REMOTE/NAME; "" for a tag or commit."""
    if ref in info.get("local", ()):
        return ref
    if ref in info.get("remote", ()):
        return ref.split("/", 1)[1]
    return ""


def _git_ro(d, *a, timeout=10):
    """Read-only git in d that takes no lock (not even the index's) and never asks
    for a password: (ok, stdout, why it failed)."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    try:
        r = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", "-C", d, *a],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=max(1, timeout),
                           stdin=subprocess.DEVNULL, env=env, **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "", "git took too long"
    except (OSError, subprocess.SubprocessError) as e:
        return False, "", f"git did not run: {e}"
    err = [x.strip() for x in r.stderr.splitlines() if x.strip()]
    return r.returncode == 0, r.stdout, (err[0] if err else f"git exited with status {r.returncode}")[:300]


def _repo_row(top, rel, nested, deadline):
    """One row of the Add form's tree: the repository at rel ("." the project)."""
    d = top if rel == "." else os.path.join(top, *rel.split("/"))
    row = {"path": rel, "depth": 0 if rel == "." else rel.count("/") + 1, "branch": None, "head": None,
           "dirty": 0, "ahead": None, "behind": None, "branches": []}

    def git(*a):
        left = deadline - time.monotonic()
        return _git_ro(d, *a, timeout=min(10, left)) if left > 0 else (False, "", "not read: out of time")
    ok, out, err = git("status", "--porcelain=v1", "-uall", "-z")
    if not ok:
        row["err"] = err
        return row
    pre, ents, i = ("" if rel == "." else rel + "/"), out.split("\0"), 0
    while i < len(ents):
        e, i = ents[i], i + 1
        if len(e) < 4:
            continue
        if "R" in e[:2] or "C" in e[:2]:   # the path it was renamed or copied from follows
            i += 1
        if (pre + e[3:]).rstrip("/") not in nested:   # a repository inside it is its own row
            row["dirty"] += 1
    ok, out, _ = git("symbolic-ref", "-q", "--short", "HEAD")
    row["branch"] = out.strip() or None if ok else None
    ok, out, _ = git("rev-parse", "--short", "HEAD")
    row["head"] = out.strip() or None if ok else None
    ok, out, _ = git("rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if ok and len(out.split()) == 2 and all(x.isdigit() for x in out.split()):
        row["behind"], row["ahead"] = (int(x) for x in out.split())
    ok, out, _ = git("for-each-ref", "--sort=-committerdate", f"--count={REPO_REFS}", "--format=%(refname)",
                     "refs/heads", "refs/remotes")
    local = [x[len("refs/heads/"):] for x in out.splitlines() if x.startswith("refs/heads/")]
    remote = [x[len("refs/remotes/"):] for x in out.splitlines()
              if x.startswith("refs/remotes/") and not x.endswith("/HEAD")]
    row["branches"] = local + remote
    return row


def repo_tree(runs, project):
    """The Add form's tree: the project's repository (".") and those nested in it
    (the queue's nested_repos, skipping the queue's folder as the diff page does),
    each with its branch, short HEAD, uncommitted entries, ahead/behind its upstream
    and its branches. Read-only git, no fetch; a repository git cannot read is a row
    with err."""
    p = project_path(project)
    if not p or not os.path.isabs(p) or not os.path.isdir(p):
        return {"error": "not a folder (give an absolute path)"}
    ok, top, _ = _git_ro(p, "rev-parse", "--show-toplevel")
    top = top.strip()
    if not ok or not top:
        return {"error": "not in a git repository"}
    if os.path.normcase(os.path.realpath(top)) != os.path.normcase(os.path.realpath(p)):
        return {"error": f"not the repository's top folder, which is {top}"}
    out = {"project": top, "more": 0}
    tq = _tq()
    try:
        nested = tq.nested_repos(top, skip=(runs.queue_dir,)) if tq else []
    except Exception:   # an odd folder: the project's own row still shows
        nested, out["warn"] = [], "the repositories inside it could not be listed"
    out["more"] = max(0, len(nested) - (REPO_ROWS - 1))
    deadline, every = time.monotonic() + REPO_BUDGET, set(nested)
    out["repos"] = [_repo_row(top, rel, every, deadline) for rel in [".", *nested[:REPO_ROWS - 1]]]
    return out


def repo_args(form, project, many=False):
    """The tree's choices as harness queue add takes them: (whether the project's
    own changes are left uncommitted, the --repo-branch args, why not or "")."""
    root = _one(form, "repo_mode:.") or "branch"   # no tree on the form (it did not load): as before
    if root not in REPO_MODES:
        return False, [], "the project: not a choice: " + root[:40]
    rels = sorted(k[len("repo_mode:"):] for k in form if k.startswith("repo_mode:") and k != "repo_mode:.")
    if len(rels) > REPO_ROWS:
        return False, [], f"at most {REPO_ROWS} repositories"
    real = os.path.realpath(project)
    args = []
    for rel in rels:
        mode, name = _one(form, "repo_mode:" + rel), rel[:80]
        if mode not in REPO_MODES:
            return False, [], f"{name}: not a choice: {mode[:40]}"
        if mode != "branch":   # left uncommitted: the queue's default
            continue
        d = os.path.join(project, *rel.split("/"))
        if (not rel or rel.startswith("/") or posixpath.normpath(rel) != rel or ".." in rel.split("/")
                or re.search(r"[\\:=\x00-\x1f]", rel) or not os.path.isdir(d)
                or not os.path.exists(os.path.join(d, ".git"))
                or not os.path.realpath(d).startswith(real.rstrip(os.sep) + os.sep)):
            return False, [], f"not a repository inside the project: {name}"
        br, tgt = _one(form, "repo_branch:" + rel).strip(), _one(form, "repo_target:" + rel).strip()
        for what, v in (("branch", br), ("target branch", tgt)):
            if v and not ref_ok(v):
                return False, [], f"{name}: {what}: not a branch name: {v[:80]}"
        if br and many:
            return False, [], f"{name}: a branch name is for one ticket; leave it empty or add the tickets one at a time"
        args.append(f"--repo-branch={rel}={br}:{tgt}" if tgt else f"--repo-branch={rel}={br}" if br
                    else f"--repo-branch={rel}")
    return root == "uncommitted", args, ""


def add_tickets(runs, form):
    """The queue page's Add form: harness queue add, its values checked first."""
    raw = _one(form, "keys")
    keys = [k.upper() for k in re.split(r"[\s,;]+", raw) if k]
    if not keys:
        return False, "give at least one ticket key"
    if len(keys) > MAX_KEYS:
        return False, f"at most {MAX_KEYS} tickets at a time"
    bad = [k for k in keys if not KEY_RE.match(k) or len(k) > 40]
    if bad:
        return False, "not a ticket key (like ABC-123): " + ", ".join(bad[:5])[:200]
    project = project_path(_one(form, "project"))
    if not project or not os.path.isabs(project) or not os.path.isdir(project):
        return False, "the project folder must be an absolute path to a folder that exists"
    keep, repo_branches, why = repo_args(form, project, len(keys) > 1)
    if why:
        return False, why
    if keep and any(_one(form, n).strip() for n in ("base", "ref", "branch", "start", "cont")):
        return False, ("the project's changes are left uncommitted, so there is no starting point, target branch "
                       "or work branch for it; pick changes on a branch for the project, or leave those empty")
    base, ref, branch = (_one(form, n).strip() for n in ("base", "ref", "branch"))
    wait = _one(form, "after_plan") != "go"
    mode = _one(form, "start")
    if mode and mode not in START_MODES:
        return False, "not a starting point: " + mode[:40]
    if mode == "continue":
        ref = _one(form, "cont").strip()
        if not ref:
            return False, "continue existing work: name the branch (or commit) the work is on"
    elif mode == "here":
        ref = "here"
    elif mode == "new":
        ref = base      # empty: the queue's own default (the folder's branch, or where a queued ticket started)
    for name, v in (("target branch" if mode else "base", base),
                    ("continue from" if mode == "continue" else "start from", "" if ref == "here" else ref),
                    ("branch", branch)):
        if v and not ref_ok(v):
            return False, f"{name}: not a branch, tag or commit name: {v[:80]}"
    if branch and len(keys) > 1:
        return False, "a work branch name is for one ticket; add the tickets one at a time"
    if mode == "continue" and len(keys) > 1:
        return False, "continue existing work takes one ticket at a time (each needs its own branch)"
    if mode == "continue" and not branch:
        branch = _branch_of_ref(project_branches(project), ref)
    preset = _one(form, "preset") or "full"
    if preset in PRESETS:
        stages = list(PRESETS[preset])
    elif preset == "custom":
        stages = [s for s in STAGES if _one(form, "stage_" + s)]
    else:
        return False, "not a preset: " + preset[:40]
    if not stages:
        return False, "pick at least one stage"
    if "fix" in stages and "review" not in stages:
        return False, "fix runs only after a review asks for fixes; pick review too"
    if "implement" not in stages and any(s in stages for s in ("review", "fix", "push")) \
            and mode == "new":
        return False, ("with no implement stage the work must already exist: pick Continue existing work and name "
                       "its branch (e.g. origin/ABC-123), or The folder as it is now")
    if "plan" not in stages or stages == ["plan"]:   # nothing waits on an approval
        wait = False
    if len(keys) == 1 and not keep:   # what the queue will not commit straight onto, said the form's way
        info = project_branches(project)
        work = branch or keys[0]
        tgt = base or info.get("current", "")
        names = {"main", "master", *info.get("defaults", ())}
        names |= {tgt} | {tgt[len(r) + 1:] for r in info.get("remotes", ()) if tgt.startswith(r + "/")}
        if "error" not in info and work in names - {""}:
            if not base and work == tgt and work not in ("main", "master", *info.get("defaults", ())):
                return False, (f"target branch: empty means the branch the folder is on, {tgt}, which is the branch "
                               "the work would be committed to; give the branch this work is meant to go into")
            return False, f"the work would be committed straight onto {work}; give it a work branch name of its own"
    note = _one(form, "note").replace("\r\n", "\n")
    if len(note) > ADD_NOTE_MAX:
        return False, f"the note is over {ADD_NOTE_MAX} characters"
    args = ["add", *keys, f"--project={project}", "--stages=" + ",".join(stages)]
    if base:
        args.append(f"--base={base}")
    if ref:
        args.append(f"--start={ref}")
    if branch:
        args.append(f"--branch={branch}")
    if keep:
        args.append("--no-branch")
    args += repo_branches
    args.append("--review-plan" if wait else "--no-plan-approval")
    if note.strip():
        args.append(f"--note={note}")
    return _queue(runs, args)


def queue_action(runs, action, form):
    if not runs.queue_dir or action not in QUEUE_ACTIONS:
        return False, "no queue, or no such action"
    if action == "add":
        return add_tickets(runs, form)
    if action == "halt":
        return _queue(runs, ["halt"])
    if action == "end":   # a run that does not stop or halt: kill it, put its ticket back
        return _queue(runs, ["halt", "--force", "--harness", runs.harness])
    mode = _one(form, "mode") or "container"
    if mode in ("host", "single") and _one(form, "confirm_host") != "1":
        return False, ("host mode runs the agents as you, with no container and no firewall: tick "
                       "'host mode is OK' to start it")
    return runs.start_runner(mode)


def session_stop(runs, sid):
    """The Stop button of a queue stage's session: stop its ticket. (ok, message)."""
    src, row = runs.find(sid)
    if not row:
        return False, "no such session"
    root = runs.root_of(src, row) or row
    key = runs.ticket_of_row(root)
    if not key:
        return False, ("this is not a queue stage; stop a harness -p run from its terminal, "
                       "and an interactive one from opencode")
    return ticket_action(runs, key, "stop", {})


def ticket_file(runs, key, name):
    if not KEY_RE.match(key or "") or not OUT_RE.match(name or "") or key not in runs._ticket_dirs():
        return None
    try:
        with open(os.path.join(runs.queue_dir, key, name), "rb") as f:
            return f.read()
    except OSError:
        return None


def _queue_line(runs):
    """The queue in a line: what its run is on, and what waits on the owner."""
    r = runs.runner()
    counts = {}
    for k in runs.ticket_keys():
        status = runs.ticket_state(k).get("status", "")
        counts[status] = counts.get(status, 0) + 1
    if r["running"]:
        what = (f"on <b>{esc(r['ticket'])}</b> &middot; {esc(r.get('stage') or '')}" if r.get("ticket")
                else "between tickets")
        head = f"<b>Queue {'halting' if r['halting'] else 'running'}</b> {what}"
    elif r["starting"]:
        head = "<b>Queue starting&hellip;</b>"
    else:
        head = "<b>Queue not running</b>" + (" (start it on the queue page)" if counts.get("queued") else "")
    waiting = sum(counts.get(s, 0) for s in WAITING + ("interrupted",))
    tail = [f"{counts['queued']} queued"] if counts.get("queued") else []
    if waiting:
        tail.append(f"<b>{waiting} waiting on you</b>")
    return (f"<a href='/runs/queue'>{head}</a>" + (" &middot; " + ", ".join(tail) if tail else ""))


def live_summary(runs):
    """The index's panel: live runs, linked."""
    live = runs.live()
    rows = [r for r in runs.sessions(limit=100) if r["id"] in live]
    parts = ["<section class='panel'><h2>Agent runs</h2>"]
    if runs.queue_dir:
        parts.append(f"<div>{_queue_line(runs)}</div>")
    if rows:
        parts.append("<ul class='runs'>")
        now = time.time()
        parts += [_row(runs, r, live, now=now) for r in rows]
        parts.append("</ul>")
    else:
        parts.append("<div class='muted'>No agent session is live right now.</div>")
    parts.append("<div class='muted'><a href='/runs/queue'>The queue</a> &middot; "
                 "<a href='/runs'>All sessions, transcripts and queue stages &rarr;</a></div></section>")
    return parts


# --- /runs/<session> ------------------------------------------------------------

def render_side(runs, src, row, live, sent="", todos=None, oc_url=""):
    sid = row["id"]
    now = time.time()
    root = runs.root_of(src, row) or row
    parts = []
    key = runs.ticket_of_row(root)
    if key:
        st = runs.ticket_state(key)
        stage, n = _stage_label(runs, key, root, st, None)
        working = root["id"] in live or now - _ms(root.get("time_updated")) / 1000 <= ACTIVE
        parts.append(f"<div class='ph'><span class='muted'>Queue ticket</span>"
                     f"<a href='/runs/ticket/{quote(key)}'><b>{esc(key)}</b></a>"
                     f"{_pill(st.get('status', ''), st.get('stopping'))}"
                     + (f"<span class='tag'>{esc(stage)}{f' &middot; {n}' if n else ''}</span>" if stage else "")
                     + "</div>")
        if st.get("status") == "running" and working and not st.get("stopping"):
            parts.append(_btn(f"/runs/{quote(sid)}/stop", "Stop this stage", "warn", confirm=(
                f"Stop {key}? Its agent is stopped and the ticket is held; Resume on its page takes it again.")))
    if oc_url:
        parts.append(f"<a class='btn' href='{esc(oc_url)}' target='_blank' rel='noreferrer'>Open in opencode web</a>")
    if sid in live:
        parts.append(_send_form(sid, sent))
    if todos:
        n = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        parts.append(f"<h2>Todo <span class='muted'>{n}/{len(todos)} done</span></h2>{_todos(todos)}")
    parts.append("<dl class='facts'>")
    facts = [("status", None), ("source", src.label), ("folder", row.get("directory")),
             ("agent", row.get("agent")), ("model", _model_name(row.get("model"))),
             ("started", _when(_ms(row.get("time_created")))),
             ("updated", f"{_when(_ms(row.get('time_updated')))} ({_ago(_ms(row.get('time_updated')), now)})"),
             ("tokens", f"{_ms(row.get('tokens_input')):,} in / {_ms(row.get('tokens_output')):,} out"),
             ("session", sid)]
    for k, v in facts:
        if k == "status":
            parts.append(f"<dt>status</dt><dd>{_status(runs, row, live, now)}</dd>")
        elif v:
            parts.append(f"<dt>{k}</dt><dd>{esc(str(v))}</dd>")
    parts.append("</dl>")
    kids = runs.children(src, sid)
    if kids:
        parts.append(f"<h2>Subagent sessions <span class='muted'>{len(kids)}</span></h2><ul class='runs'>")
        for k in kids:
            parts.append(f"<li><span><a href='/runs/{quote(k['id'])}'>{esc(k.get('title') or k['id'])}</a></span>"
                         f"<span class='when'>{_ago(_ms(k['time_updated']), now)}</span>"
                         f"<span class='meta'>{esc(k.get('agent') or '')}</span></li>")
        parts.append("</ul>")
    if sid in live:
        pass
    elif root and root["id"] != sid and root["id"] in live:
        parts.append("<h2>Message the agent</h2>")
        parts.append(f"<div class='muted'>This is a subagent of a live run; message the "
                     f"<a href='/runs/{quote(root['id'])}'>main session</a>.</div>")
    else:
        parts.append("<h2>Message the agent</h2>")
        parts.append("<div class='muted'>Read-only: this run has finished, or it is an interactive "
                     "session (type in its terminal or opencode web). Headless runs (<code>harness -p</code>, "
                     "<code>harness queue</code>) take messages while they work.</div>")
    items = runs.inbox(src, sid)
    if items:
        parts.append("<ul class='inbox'>")
        for ts, sender, state, text in items[:20]:
            cls = " class='bad'" if state.startswith("failed") or state == "late" else ""
            short = text if len(text) <= 160 else text[:160] + "…"
            parts.append(f"<li{cls}>{esc(_when(ts))} {esc(sender)} <b>{esc(state)}</b>: {esc(short)}</li>")
        parts.append("</ul>")
    return "".join(parts)


def _send_form(sid, sent=""):
    return ("<h2>Message the agent</h2>"
            f"<form method='post' action='/runs/{quote(sid)}/send' class='send'>"
            "<textarea name='text' required placeholder='Steer it, answer it, or tell it to stop: the agent "
            "reads this at its next step.'></textarea><button type='submit' class='primary'>Send</button></form>"
            + (f"<div class='muted'>{esc(sent)}</div>" if sent else ""))


# Every runs page: a form or button with data-confirm asks first; the add form's
# presets tick their stages; a ?done= flash is not shown again on reload.
COMMON_JS = """
(function () {
  document.addEventListener('submit', function (e) {
    var f = e.target, m = f.getAttribute('data-confirm');
    if (!m && f.getAttribute('data-confirm-host')) {
      var md = f.querySelector('input[name=mode]:checked');
      if (md && md.value !== 'container') m = f.getAttribute('data-confirm-host');
    }
    if (m && !window.confirm(m)) { e.preventDefault(); return; }
    var b = e.submitter || f.querySelector('button[type=submit]');
    if (b) setTimeout(function () { b.disabled = true; b.setAttribute('data-busy', '1'); }, 0);
  }, true);
  // Back to a page the browser kept: its buttons are not still busy.
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('[data-busy]').forEach(function (b) { b.disabled = false; b.removeAttribute('data-busy'); });
  });
  // The top bar's freshness note: 'ok' after each good poll, 'off' when a
  // poll failed, 'auth' when the session ended. A page that polls calls it;
  // a page that does not shows nothing.
  var fresh = document.getElementById('fresh'), okAt = 0, state = '', stale = false;
  function paint() {
    if (!fresh || !state) return;
    var age = okAt ? Math.round((Date.now() - okAt) / 1000) : 0, t = '', c = 'fresh';
    if (state === 'auth') { fresh.innerHTML = 'signed out &middot; <a href="/login">sign in</a>'; fresh.className = 'fresh bad'; fresh.hidden = false; return; }
    if (state === 'off') { t = 'offline \\u00b7 retrying' + (okAt ? ' \\u00b7 last update ' + ago(age) : ''); c += ' bad'; }
    else t = age < 20 ? 'live' : 'updated ' + ago(age);
    if (stale) { t += ' \\u00b7 harness changed on disk: restart harness serve'; c += ' warn'; }
    fresh.textContent = t; fresh.className = c; fresh.hidden = false;
  }
  function ago(s) { return s < 90 ? s + ' s ago' : s < 5400 ? Math.round(s / 60) + ' min ago' : Math.round(s / 3600) + ' h ago'; }
  window.hFresh = function (s) { state = s; if (s === 'ok') okAt = Date.now(); paint(); };
  setInterval(paint, 1000);
  // fetch for the live JSON: rejects with 'auth' when the session ended (the
  // server answers 401, or a proxy sent the login page) and 'off' otherwise.
  window.hFetch = function (url) {
    return fetch(url, {credentials: 'same-origin', cache: 'no-store', headers: {'X-Harness-Live': '1'}}).then(function (r) {
      var ct = r.headers.get('Content-Type') || '';
      if (r.status === 401 || r.redirected || (r.ok && ct.indexOf('json') < 0)) throw 'auth';
      if (!r.ok) throw 'off';
      stale = r.headers.get('X-Harness-Stale') === '1';
      return r.json();
    }).then(function (d) { window.hFresh('ok'); return d; }, function (e) {
      window.hFresh(e === 'auth' ? 'auth' : 'off');
      throw e;
    });
  };
  // Replace a region's HTML but keep any form in it that is being filled in
  // (a changed field or the focus), matched to the new HTML's form by action
  // and position. Returns false, changing nothing, when a kept form has no
  // place in the new HTML or a changed field is outside any form.
  function changed(x) {
    if (x.type === 'hidden' || x.type === 'submit' || x.type === 'button') return false;
    if (x.type === 'checkbox' || x.type === 'radio') return x.checked !== x.defaultChecked;
    if (x.tagName === 'SELECT') {
      for (var j = 0; j < x.options.length; j++) if (x.options[j].selected !== x.options[j].defaultSelected) return true;
      return false;
    }
    return x.value !== x.defaultValue;
  }
  function busy(el) {
    var a = document.activeElement;
    if (a && a !== document.body && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return true;
    var f = el.querySelectorAll('input, textarea, select');
    for (var i = 0; i < f.length; i++) if (changed(f[i])) return true;
    return false;
  }
  function keys(root) {
    var n = {}, out = [];
    root.querySelectorAll('form').forEach(function (f) {
      var k = (f.getAttribute('action') || '') + '|' + (f.id || '');
      n[k] = (n[k] || 0) + 1;
      out.push([k + '#' + n[k], f]);
    });
    return out;
  }
  window.hSwap = function (r, html) {
    var keep = {}, any = false, loose = false;
    keys(r).forEach(function (p) { if (busy(p[1])) { keep[p[0]] = p[1]; any = true; } });
    r.querySelectorAll('input, textarea, select').forEach(function (x) { if (!x.form && changed(x)) loose = true; });
    if (loose) return false;
    var tmp = document.createElement('div');
    tmp.innerHTML = html;
    if (any) {
      var slots = {};
      keys(tmp).forEach(function (p) { slots[p[0]] = p[1]; });
      for (var k in keep) if (!slots[k]) return false;
      var a = document.activeElement, s = null, top = 0;
      if (a && r.contains(a)) { try { s = [a.selectionStart, a.selectionEnd]; } catch (e) {} top = a.scrollTop; } else a = null;
      for (k in keep) slots[k].parentNode.replaceChild(keep[k], slots[k]);
    }
    var open = {};
    r.querySelectorAll('details[data-k]').forEach(function (d) { open[d.getAttribute('data-k')] = d.open; });
    tmp.querySelectorAll('details[data-k]').forEach(function (d) {
      var n = d.getAttribute('data-k');
      if (n in open) d.open = open[n];
    });
    while (r.firstChild) r.removeChild(r.firstChild);
    while (tmp.firstChild) r.appendChild(tmp.firstChild);
    if (a && document.contains(a)) {
      a.focus({preventScroll: true});
      if (s && s[0] != null) try { a.setSelectionRange(s[0], s[1]); } catch (e) {}
      a.scrollTop = top;
    }
    return true;
  };
  if (/[?&]done=/.test(location.search) && history.replaceState)
    history.replaceState(null, '', location.pathname + location.hash);
  var add = document.getElementById('addform');
  function toAdd(smooth) {
    var p = document.getElementById('add');
    p.scrollIntoView({behavior: smooth ? 'smooth' : 'auto', block: 'start'});
    var k = add.querySelector('input[name=keys]');
    if (k) k.focus({preventScroll: true});
    p.classList.add('hl');
    setTimeout(function () { p.classList.remove('hl'); }, 1500);
  }
  if (add) {
    document.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href="#add"]');
      if (a) { e.preventDefault(); toAdd(true); }
    });
    if (document.getElementById('add').hasAttribute('data-err')) toAdd(false);
    // Every choice that would be refused is off, with why; Add stays off while
    // anything above would be refused, and says what.
    var presets = %PRESETS%, info = null, projErr = '';
    var checks = add.querySelectorAll('input[name^=stage_]');
    var hint = document.getElementById('addhint'), btn = add.querySelector('button[type=submit]');
    var KEY = /^[A-Z][A-Z0-9_]*-[0-9]+$/, MAXK = %MAXK%, tree = document.getElementById('repotree');
    var REF = /^[A-Za-z0-9_][A-Za-z0-9._\/-]{0,199}$/;
    function refOk(v) {   // ref_ok
      return REF.test(v) && v.indexOf('..') < 0 && v.indexOf('//') < 0 && !/(\/|\.lock|\.)$/.test(v);
    }
    function q(sel) { return add.querySelector(sel); }
    function pick(name, v) { return q('input[name=' + name + '][value=' + v + ']'); }
    function val(name) { var r = q('input[name=' + name + ']:checked'); return r ? r.value : ''; }
    function say(node, why) { if (node) { node.textContent = why || ''; node.hidden = !why; } }
    function off(input, why) {
      input.disabled = !!why;
      if (why) input.checked = false;
      say(input.closest('label').querySelector('.why'), why);
    }
    function branchOf(ref) {   // the branch continuing ref ends on, as the server works it out
      if (!info) return '';
      if (info.local.indexOf(ref) >= 0) return ref;
      return info.remote.indexOf(ref) >= 0 ? ref.split('/').slice(1).join('/') : '';
    }
    function shared(b, tgt) {   // what the queue will not commit straight onto (shared_branches)
      if (b === 'main' || b === 'master') return 'a main branch';
      if (info && info.defaults.indexOf(b) >= 0) return "a remote's default branch";
      var plain = tgt;
      (info ? info.remotes : []).forEach(function (r) { if (tgt.indexOf(r + '/') === 0) plain = tgt.slice(r.length + 1); });
      return b === tgt || b === plain ? 'the target branch' : '';
    }
    function presetOf() {
      var on = [], hit = 'custom';
      checks.forEach(function (c) { if (c.checked) on.push(c.value); });
      Object.keys(presets).forEach(function (k) { if (presets[k].join() === on.join()) hit = k; });
      return hit;
    }
    function sync() {
      var keys = q('input[name=keys]').value.toUpperCase().split(/[\\s,;]+/).filter(Boolean), many = keys.length > 1;
      off(q('input[name=stage_fix]'), q('input[name=stage_review]').checked ? '' : 'fix needs review');
      var on = [];
      checks.forEach(function (c) { if (c.checked) on.push(c.value); });
      function has(s) { return on.indexOf(s) >= 0; }
      say(q('#stagechecks').nextElementSibling, q('input[name=stage_review]').checked ? '' :
        'fix needs review: it fixes what the review asks for.');
      // with nothing to implement, the work must already exist
      var existing = !has('implement') && (has('review') || has('fix') || has('push'));
      off(pick('start', 'new'), existing ? 'Not with these stages: a new branch has no work on it yet to ' +
        (has('review') ? 'review' : 'push') + '.' : '');
      off(pick('start', 'continue'), many ? 'One ticket at a time: each one needs its own branch.' : '');
      // the project's changes left uncommitted: no starting point, target or work branch for it
      var root = q('select[name="repo_mode:."]'), keep = !!root && root.value === 'uncommitted';
      var fs = document.getElementById('f-start'), bf = document.getElementById('f-branch');
      fs.hidden = fs.disabled = bf.hidden = keep;
      say(q('#f-repos > .why'), keep ? "The project's changes are left uncommitted, so Starting point, Target " +
        'branch and Work branch name are off.' : '');
      var md = keep ? '' : val('start'), cont = q('input[name=cont]'), br = q('input[name=branch]'),
        base = q('input[name=base]');
      cont.disabled = md !== 'continue';
      base.disabled = keep;
      br.disabled = many || keep;
      say(document.getElementById('branchwhy'), many ? 'Off with more than one ticket: each gets a branch named ' +
        'after its key.' : '');
      br.placeholder = md === 'continue' ? 'the branch you continue' : 'the ticket key';
      var ap = document.getElementById('f-after_plan'), apWhy = !has('plan') ? 'No plan stage: there is no plan to approve.' :
        on.length === 1 ? 'Plan only: the ticket is done once the plan is written.' : '';
      ap.disabled = !!apWhy;
      say(ap.querySelector(':scope > .why'), apWhy);
      var probs = [], warns = [];
      var bad = keys.filter(function (k) { return !KEY.test(k) || k.length > 40; });
      if (!keys.length) probs.push('Give at least one ticket key.');
      else if (bad.length) probs.push('Not a ticket key (like ABC-123): ' + bad.slice(0, 3).join(', '));
      else if (keys.length > MAXK) probs.push('At most ' + MAXK + ' tickets at a time.');
      if (!q('input[name=project]').value.trim()) probs.push('Give the project folder.');
      else if (projErr) probs.push(projErr);
      if (!on.length) probs.push('Pick at least one stage.');
      if (!md && !keep) probs.push(!existing ? 'Pick a starting point.' : many ? 'Pick The folder as it is now: the work must ' +
        'already exist, and Continue existing work takes one ticket at a time.' :
        'Pick where the existing work is: Continue existing work, or The folder as it is now.');
      if (md === 'continue' && !cont.value.trim()) probs.push('Continue existing work: name the branch the work is on.');
      tree.querySelectorAll('select[name^="repo_mode:"]').forEach(function (s) {
        var box = s.parentNode.querySelector('.rbr'), on = s.value === 'branch', rel = s.name.slice(10);
        if (!box) return;
        box.hidden = !on;
        box.querySelectorAll('input').forEach(function (i) {
          var own = i.name.indexOf('repo_branch:') === 0, v = i.value.trim();
          i.disabled = !on || own && many;
          if (own) i.placeholder = many ? "each ticket's key" : keys[0] || 'the ticket key';
          if (!i.disabled && v && !refOk(v))
            probs.push(rel + ': ' + (own ? 'branch' : 'target branch') + ': not a branch name: ' + v);
        });
      });
      if (info && !many && !keep) {
        var tgt = base.value.trim() || info.current, own = br.value.trim();
        var work = own || (md === 'continue' ? branchOf(cont.value.trim()) : '') || keys[0] || '';
        var why = work ? shared(work, tgt || '') : '';
        if (why === 'the target branch' && !base.value.trim())
          probs.push('Target branch: empty means the branch the folder is on, ' + tgt + ', which is the branch the work ' +
            'would be committed to. Give the branch this work is meant to go into (e.g. ' + (info.defaults[0] || 'main') + ').');
        else if (why)
          probs.push('The work would be committed straight onto ' + work + ' (' + why + '). ' + (own ?
            'Give another work branch name.' : 'Open Work branch name and give it a branch of its own.'));
      }
      if (info && md !== 'continue' && !keep) keys.forEach(function (k) {
        var name = (!many && br.value.trim()) || k, there = [];
        if (info.local.indexOf(name) >= 0) there.push(name);
        info.remote.forEach(function (r) { if (r.split('/').slice(1).join('/') === name) there.push(r); });
        if (there.length && warns.length < 3) warns.push('A branch ' + name + ' already exists (' + there.join(', ') + '). To build on it ' +
          'pick Continue existing work; if it has commits the start does not, the add is refused.');
      });
      hint.innerHTML = '';
      (probs.length ? probs : warns.length ? warns : [on.length ? 'Stages: ' + on.join(' \\u2192 ') : '']).forEach(function (t) {
        var d = document.createElement('div');
        d.textContent = t;
        d.className = probs.length ? 'err' : warns.length ? 'warn' : 'muted';
        hint.appendChild(d);
      });
      if (probs.length && warns.length) warns.forEach(function (t) {
        var d = document.createElement('div'); d.textContent = t; d.className = 'warn'; hint.appendChild(d);
      });
      btn.disabled = probs.length > 0;
      btn.title = probs.length ? 'Not yet: ' + probs[0] : '';
    }
    add.addEventListener('change', function (e) {
      var t = e.target;
      if (t.name === 'preset' && presets[t.value])
        checks.forEach(function (c) { c.checked = presets[t.value].indexOf(c.value) >= 0; });
      sync();
      if (t.name && t.name.indexOf('stage_') === 0) pick('preset', presetOf()).checked = true;
      if (t.name === 'start' && t.value === 'continue') q('input[name=cont]').focus();
    });
    add.addEventListener('input', function (e) {
      if (e.target.name === 'cont' && e.target.value) pick('start', 'continue').checked = true;
      sync();
    });
    sync();
    // The project's repositories, how the ticket's changes land in each: the project's own
    // row drives the branch fields above; a nested one on a branch has its own branch and target.
    var treeFs = document.getElementById('f-repos'), sent = JSON.parse(tree.getAttribute('data-sent') || '{}'),
      MODES = [['uncommitted', 'changes: left uncommitted'], ['branch', 'changes: on a branch']];
    function el(tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }
    function repoRow(r, i, n) {
      var d = el('div', 'rrow');
      d.style.paddingLeft = (10 + 18 * r.depth) + 'px';
      d.appendChild(el('code', 'rpath', r.path === '.' ? './ (the project)' : r.path + '/'));
      if (r.err) { d.appendChild(el('span', 'err', r.err)); return d; }
      d.appendChild(el('span', '', r.branch ? 'on ' + r.branch : r.head ? 'detached at ' + r.head : 'no commits yet'));
      var dirty = d.appendChild(el('span', r.dirty ? 'hot' : '', r.dirty + ' uncommitted'));
      if (r.dirty) dirty.title = "files you have here that the ticket's changes could mix with";
      d.appendChild(el('span', '', r.ahead == null ? 'no upstream' : '\u2191' + r.ahead + ' \u2193' + r.behind));
      var s = el('select'), want = sent['repo_mode:' + r.path];
      s.name = 'repo_mode:' + r.path;
      s.setAttribute('aria-label', 'how the changes land in ' + r.path);
      MODES.forEach(function (m) { var o = s.appendChild(el('option', '', m[1])); o.value = m[0]; });
      // the project with none inside it: on a branch, as before
      s.value = want === 'branch' || want === 'uncommitted' ? want : r.path === '.' && n === 1 ? 'branch' : 'uncommitted';
      d.appendChild(s);
      if (r.path === '.') return d;
      var box = el('span', 'rbr'), list = el('datalist');
      list.id = 'rb-' + i;
      r.branches.forEach(function (b) { list.appendChild(el('option')).value = b; });
      [['repo_branch:', 'Branch', ''], ['repo_target:', 'Target', r.branch || '']].forEach(function (f) {
        var l = box.appendChild(el('label', '', f[1] + ' ')), inp = l.appendChild(el('input'));
        inp.name = f[0] + r.path;
        inp.spellcheck = false;
        inp.autocomplete = 'off';
        inp.value = inp.name in sent ? sent[inp.name] : f[2];
        if (f[0] === 'repo_target:') inp.setAttribute('list', list.id);
      });
      box.appendChild(list);
      d.appendChild(box);
      return d;
    }
    function repos(p) {
      tree.innerHTML = '';
      treeFs.hidden = true;
      sync();
      if (!p) return;
      fetch('/runs/queue/repos?project=' + encodeURIComponent(p), {credentials: 'same-origin', cache: 'no-store'})
        .then(function (r) { return r.json(); }).then(function (d) {
          if (proj.value.trim() !== p) return;
          tree.innerHTML = '';
          if (d.error) tree.appendChild(el('div', 'rrow err', d.error));
          else d.repos.forEach(function (r, i) { tree.appendChild(repoRow(r, i, d.repos.length)); });
          if (d.more) tree.appendChild(el('div', 'rrow', d.more + ' more not listed: their changes are left uncommitted'));
          if (d.warn) tree.appendChild(el('div', 'rrow err', d.warn));
          sent = {};
          treeFs.hidden = false;
          sync();
        }).catch(function () {});
    }
    // The project's branches: suggestions for the branch fields, and what the folder is on.
    var proj = add.querySelector('input[name=project]'), dl = document.getElementById('branches'),
      ph = document.getElementById('projhint'), bt = null, lastP = null;
    function branches() {
      var p = proj.value.trim();
      if (p === lastP) return;
      lastP = p;
      repos(p);
      info = null; projErr = '';
      if (!p) { ph.textContent = ''; sync(); return; }
      fetch('/runs/queue/branches?project=' + encodeURIComponent(p), {credentials: 'same-origin', cache: 'no-store'})
        .then(function (r) { return r.json(); }).then(function (d) {
          if (proj.value.trim() !== p) return;
          dl.innerHTML = '';
          if (d.error) {
            ph.textContent = p + ': ' + d.error; ph.className = 'projhint err';
            projErr = 'Project folder: ' + d.error + '.'; sync(); return;
          }
          info = d;
          ph.className = 'projhint muted';
          ph.textContent = 'git repository ' + d.top + (d.current ? ', on branch ' + d.current : ', on a detached commit') +
            ' \u00b7 ' + d.local.length + ' local and ' + d.remote.length + ' remote branches';
          d.local.concat(d.remote).forEach(function (b) {
            var o = document.createElement('option'); o.value = b; dl.appendChild(o);
          });
          add.querySelector('input[name=base]').placeholder = d.current ? d.current + ' (the branch the folder is on)' :
            'the commit the folder is on';
          sync();
        }).catch(function () { lastP = null; });
    }
    proj.addEventListener('input', function () { clearTimeout(bt); bt = setTimeout(branches, 400); });
    proj.addEventListener('change', branches);
    branches();
  }
})();
""".replace("%PRESETS%", json.dumps({k: list(v) for k, v in PRESETS.items()})).replace("%MAXK%", str(MAX_KEYS))

# The home, queue, ticket and sessions pages: poll the page's live JSON every 3 s
# and swap the regions that changed (hSwap keeps a form being filled in and open
# <details>). No polling while the tab is hidden.
LIVE_JS = """
(function () {
  var tag = document.getElementById('live');
  if (!tag) return;
  var url = tag.getAttribute('data-live'), sig = tag.getAttribute('data-sig'), last = {}, seeded = false,
    timer = null, fails = 0;
  window.hFresh('ok');
  function apply(regions) {
    Object.keys(regions).forEach(function (k) {
      var r = document.getElementById('r-' + k), h = regions[k];
      if (r && last[k] !== h && window.hSwap(r, h)) last[k] = h;
    });
  }
  function later(ms) { clearTimeout(timer); timer = document.hidden ? null : setTimeout(tick, ms); }
  function tick() {
    timer = null;
    if (document.hidden) return;
    window.hFetch(url).then(function (d) {
      fails = 0;
      if (d.gone) { location.replace(d.gone); return; }
      var regions = d.regions || {};
      // The page as served is what the first poll would show: nothing to swap.
      if (!seeded) { seeded = true; if (d.sig === sig) Object.keys(regions).forEach(function (k) { last[k] = regions[k]; }); }
      apply(regions);
      later(3000);
    }).catch(function () { fails++; later(fails > 3 ? 15000 : 6000); });
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) later(200); });
  later(3000);
})();
"""

POLL_JS = """
(function () {
  var box = document.getElementById('msgs'), side = document.getElementById('side'), nav = document.getElementById('r-nav');
  if (!box) return;
  var url = box.getAttribute('data-tail'), follow = box.getAttribute('data-follow'), idle = 0, last = '', navLast = nav ? nav.innerHTML : '', sideLast = null;
  window.hFresh('ok');
  function nearBottom() { return window.innerHeight + window.scrollY >= document.body.scrollHeight - 160; }
  function tick() {
    if (document.hidden) { setTimeout(tick, 3000); return; }
    var n = box.querySelectorAll('.msg').length, from = Math.max(0, n - 1);
    window.hFetch(url + '?from=' + from).then(function (d) {
      var stick = nearBottom();
      if (d.sig !== last) {
        last = d.sig;
        box.querySelectorAll('.msg').forEach(function (m) {
          if (+m.getAttribute('data-i') >= d.from) m.remove();
        });
        box.insertAdjacentHTML('beforeend', d.html);
        var none = document.getElementById('nomsg');
        if (none && d.count) none.remove();
        if (stick) window.scrollTo(0, document.body.scrollHeight);
      }
      if (d.side !== sideLast && window.hSwap(side, d.side)) sideLast = d.side;
      if (nav && d.nav && d.nav !== navLast && window.hSwap(nav, d.nav)) navLast = d.nav;
      idle = d.working ? 0 : idle + 1;
      // Following a queue: once this stage's session is done, go to the next one.
      if (follow && !d.working) {
        fetch(follow, {credentials: 'same-origin', cache: 'no-store'}).then(function (r) { return r.json(); })
          .then(function (f) {
            if (f.sid && f.sid !== box.getAttribute('data-sid'))
              location.replace('/runs/' + encodeURIComponent(f.sid) + '?' + follow.split('?')[1]);
          }).catch(function () {});
      }
      setTimeout(tick, idle > (follow ? 200 : 20) ? 15000 : 2500);
    }).catch(function () { setTimeout(tick, 10000); });
  }
  setTimeout(tick, 2500);
})();
"""

FOLLOW_JS = """
(function () {
  var w = document.getElementById('wait');
  if (!w) return;
  var url = w.getAttribute('data-follow'), jump = w.getAttribute('data-jump');
  function tick() {
    fetch(url, {credentials: 'same-origin', cache: 'no-store'}).then(function (r) { return r.json(); })
      .then(function (f) {
        if (f.sid) location.replace('/runs/' + encodeURIComponent(f.sid) + (jump ? '?' + url.split('?')[1] : ''));
        else setTimeout(tick, 2000);
      }).catch(function () { setTimeout(tick, 5000); });
  }
  setTimeout(tick, 1500);
})();
"""

LIST_JS = LIVE_JS


def render_detail(runs, sid, sent="", follow="", oc_url=None, msg="", bad=False):
    """(title, body parts, script) or None. follow: a follow query (follow_query) to
    move on to once this session is done. oc_url: a function giving the session's
    opencode web link from its source label, or None."""
    src, row = runs.find(sid)
    if not row:
        return None
    live = runs.live()
    msgs = runs.messages(src, sid)
    crumbs = ["<a href='/runs'>Sessions</a>"]
    key = runs.ticket_of_row(row)
    if key:
        crumbs = [f"<a href='/runs/queue'>Queue</a><span>/</span><a href='/runs/ticket/{quote(key)}'>{esc(key)}</a>"]
    for p in runs.chain(src, row):
        crumbs.append(f"<span>/</span><a href='/runs/{quote(p['id'])}'>{esc(p.get('title') or p['id'])}</a>")
    title = row.get("title") or sid
    link = oc_url(src.label, sid) if oc_url else ""
    parts = [f"<header class='ph'><div class='crumbs'>{''.join(crumbs)}</div><h1>{esc(title)}</h1>"
             f"{_status(runs, row, live)}<a class='btn sm' href='/runs/{quote(sid)}.json' target='_blank' "
             "rel='noopener'>JSON</a></header>", _flash(msg, bad),
             "<div class='cols'><div class='stack'>"
             f"<div id='msgs' data-sid='{esc(sid)}' data-tail='/runs/{quote(sid)}/tail'"
             + (f" data-follow='/runs/follow.json?{esc(follow)}'" if follow else "")
             + f">{render_messages(msgs, sid)}</div>"
             + ("" if msgs else "<div class='panel muted' id='nomsg'>No messages yet.</div>") +
             "<span id='end'></span></div>",
             f"<div class='stack stick'><section class='panel' id='side'>"
             f"{render_side(runs, src, row, live, sent, todos_of(msgs), link)}</section></div></div>"]
    return title, parts, POLL_JS


def follow_query(query):
    """The follow parameters from a parsed query string, rebuilt from checked values:
    since (ms; sessions started before it are skipped) and queue (1: queue stages
    only, and move on to the next stage). (since, queue, query string)."""
    try:
        since = max(0, int((query.get("since") or ["0"])[0]))
    except ValueError:
        since = 0
    queue = (query.get("queue") or [""])[0] == "1"
    return since, queue, f"since={since}" + ("&queue=1" if queue else "")


def follow_target(runs, since=0, queue=False):
    """The session a follow page shows: of the root sessions started at or after
    since (queue stages only with queue), a live one first, then one still
    working, then the newest. None until one has started."""
    live = runs.live()
    now = time.time()
    best = None
    for r in runs.sessions(limit=200):
        created = _ms(r["time_created"])
        if created < since or (queue and not runs.ticket_of_row(r)):
            continue
        working = r["id"] in live or now - _ms(r["time_updated"]) / 1000 <= ACTIVE
        rank = (r["id"] in live, working, created)
        if best is None or rank > best[0]:
            best = (rank, r["id"])
    return best[1] if best else None


def render_follow(follow, queue):
    """The page a launch opens before its agent's session exists."""
    what = "the queue's next stage" if queue else "the agent"
    return (f"<section class='panel' id='wait' data-follow='/runs/follow.json?{esc(follow)}'"
            + (" data-jump='1'" if queue else "") +
            f"><div class='ph'><span class='sdot run'></span><h1>Waiting for {what} to start&hellip;</h1></div>"
            "<div class='muted'>This page opens its session as soon as it starts. "
            "<a href='/runs'>All sessions</a> &middot; <a href='/runs/queue'>the queue</a></div></section>")


def tail(runs, sid, start, oc_url=None):
    src, row = runs.find(sid)
    if not row:
        return None
    live = runs.live()
    msgs = runs.messages(src, sid)
    start = max(0, min(start, len(msgs)))
    working = sid in live or time.time() - _ms(row.get("time_updated")) / 1000 <= ACTIVE
    body = render_messages(msgs, sid, start)
    side = render_side(runs, src, row, live, todos=todos_of(msgs), oc_url=oc_url(src.label, sid) if oc_url else "")
    bar = nav(runs, "queue" if runs.ticket_of_row(row) else "sessions")
    sig = hashlib.sha1(f"{start}:{len(msgs)}:{body}:{side}".encode()).hexdigest()
    return {"from": start, "count": len(msgs), "html": body, "sig": sig, "working": working, "side": side,
            "nav": bar}


def export(runs, sid):
    src, row = runs.find(sid)
    if not row:
        return None
    return {"info": {k: v for k, v in row.items()}, "source": src.label,
            "children": [k["id"] for k in runs.children(src, sid)],
            "messages": [{"id": mid, "info": info, "time_created": t, "parts": plist}
                         for mid, info, t, plist in runs.messages(src, sid)]}


def part_text(runs, sid, pid):
    if not PART_RE.match(pid or ""):
        return None
    src, row = runs.find(sid)
    if not row:
        return None
    p = runs.part(src, sid, pid)
    if p is None:
        return None
    st = p.get("state") or {}
    out = st.get("output") if st.get("status") != "error" else st.get("error")
    if out is None:
        out = p.get("text", "")
    return out if isinstance(out, str) else json.dumps(out, indent=2, ensure_ascii=False)
