#!/usr/bin/env python3
"""Copy the opencode conversations of one harness launch into the active vault.

Usage: vault_capture.py <opencode.db> <vault dir> <since epoch ms> <project dir>
                       [--launch <epoch ms>] [--ticket <KEY>] [--context <file>]...

Reads opencode's SQLite store read-only and writes one markdown file per top
level session started in <project dir> (or below it) and updated since <since>
to <vault>/.raw/sessions/, named
<YYYY-MM-DD>-<session id>.md, so a rerun rewrites the same file. The user's
and the assistant's text is kept (no reasoning, no text opencode injected),
each tool call as one line saying what it touched (the file read or edited,
the grep pattern and path, the start of a bash command; no tool output except
a subagent's report), and anything shaped like a key or token is redacted.
The whole session is rendered from the database every time: opencode's
compaction only adds a marker and a summary message and keeps every earlier
message, so text from before any number of compactions stays in the file,
with each summary under its own heading. A since of 0 captures every session
in the directory (`harness vault import`). The database is shared by every opencode
session of the user (or of every harness container), so the directory filter
is what keeps other projects' conversations out of the vault. Prints the files it wrote. Never fails the
launch: any problem is one line on stderr and exit status 0.

With --launch, each session updated since then gets `ticket:` (from
--ticket) and `context:` in its front matter: the instruction files the
launch gave the model (--context, in order). A file inside the vault is named
by its vault path (git has its history); any other one (the project's
AGENTS.md) is copied to .raw/context/<hash>.md, once per content, and named
by that copy. Older sessions keep the ticket and context they were captured
with.
"""
import datetime
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys

_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[redacted private key]"),
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[redacted key]"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), "[redacted token]"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "[redacted key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "[redacted key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted token]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 [redacted]"),
    # NAME=value, "name": "value", x-api-key: value; not token = get_token(x).
    (re.compile(r"(?i)\b([A-Z0-9_-]*(api[_-]?key|secret|token|password|passwd)[A-Z0-9_-]*['\"]?\s*[=:]\s*['\"]?)"
                r"[^\s'\"()]{8,}(?=[\s'\",;})\]]|$)"), r"\1[redacted]"),
]


def redact(text):
    for pat, repl in _SECRETS:
        text = pat.sub(repl, text)
    return text


def _iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_dir(p):
    """Compare paths across forms: C:\\x, C:/x, /c/x from the container, //c/x."""
    p = str(p or "").replace("\\", "/").rstrip("/")
    while p.startswith("//"):
        p = p[1:]
    return p.lower() if re.match(r"^[A-Za-z]:/", p) else p


def _in_dir(directory, want):
    if want == "*":  # harness vault import --all
        return True
    d, w = _norm_dir(directory), _norm_dir(want)
    return bool(w) and (d == w or d.startswith(w + "/"))


def _yaml(s):
    return json.dumps(str(s or ""), ensure_ascii=False)


_TOOL_LINES = 40       # tool lines kept per message
_TASK_REPORT = 1500    # characters of a subagent's report kept


def _short(text, n=160):
    """One line, at most n characters."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "\u2026"


def _rel(path, directory):
    """A path relative to the session's directory when it is inside it."""
    p, d = _norm_dir(path), _norm_dir(directory)
    return p[len(d) + 1:] if d and p.startswith(d + "/") else str(path or "")


def _code(text):
    """Inline code that survives backticks in the text."""
    text = str(text)
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def describe_tool(p, directory):
    """One markdown line for a tool part: what it did and what it touched."""
    name = p.get("tool") or "?"
    state = p.get("state") if isinstance(p.get("state"), dict) else {}
    a = state.get("input") if isinstance(state.get("input"), dict) else {}
    path = a.get("filePath") or a.get("file_path") or a.get("path") or ""
    rel = _code(_rel(path, directory)) if path else ""
    if name == "read":
        line = f"read {rel}" if rel else "read"
        if a.get("offset") or a.get("limit"):
            start = int(a.get("offset") or 0)
            line += f" (from line {start}" + (f", {a['limit']} lines)" if a.get("limit") else ")")
    elif name in ("edit", "write", "multiedit"):
        line = f"{name} {rel}".strip()
    elif name == "apply_patch":
        files = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", str(a.get("patchText") or ""), re.M)
        line = "apply_patch " + ", ".join(_code(_rel(f.strip(), directory)) for f in files[:8]) if files else "apply_patch"
    elif name in ("grep", "glob"):
        line = f"{name} {_code(_short(a.get('pattern'), 100))}" + (f" in {rel}" if rel else "")
        if a.get("include"):
            line += f" ({_short(a['include'], 40)})"
    elif name == "list":
        line = f"list {rel}".strip()
    elif name == "bash":
        line = f"bash {_code(_short(a.get('command')))}"
    elif name == "webfetch":
        line = f"webfetch {_short(a.get('url'), 160)}"
    elif name in ("websearch", "codesearch"):
        line = f"{name} {_code(_short(a.get('query'), 120))}"
    elif name == "task":
        line = f"task ({a.get('subagent_type') or 'agent'}): {_short(a.get('description') or a.get('prompt'), 120)}"
    elif name == "todowrite":
        todos = a.get("todos") if isinstance(a.get("todos"), list) else []
        doing = [t.get("content") for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"]
        line = f"todowrite ({len(todos)} items" + (f"; doing: {_short(doing[0], 100)})" if doing else ")")
    elif name == "skill":
        line = f"skill {_short(a.get('name'), 60)}"
    else:
        # An MCP or plugin tool: its name and its first few short arguments.
        args = [f"{k}={_short(v, 60)}" for k, v in a.items() if isinstance(v, (str, int, float, bool))][:3]
        line = name + (" " + ", ".join(args) if args else "")
    if state.get("status") == "error":
        line += " (failed)"
    if name == "task" and state.get("status") == "completed" and str(state.get("output") or "").strip():
        report = str(state["output"]).strip()
        if len(report) > _TASK_REPORT:
            report = report[:_TASK_REPORT] + "\u2026"
        line += "\n" + "\n".join("  > " + ln for ln in report.splitlines())
    return "- " + line


def render(con, sess, extra=()):
    """One session as markdown, or None when it has no conversation text.
    `extra` are front matter lines added after the standard ones."""
    sid, title, directory, created, updated, tin, tout = sess
    body = []
    msgs = con.execute("SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id",
                       (sid,)).fetchall()
    for mid, mdata in msgs:
        try:
            info = json.loads(mdata)
            role = info.get("role")
        except (TypeError, ValueError, AttributeError):
            continue
        texts, tools, compaction = [], [], False
        for (pdata,) in con.execute("SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id",
                                    (mid,)):
            try:
                p = json.loads(pdata)
            except (TypeError, ValueError):
                continue
            if p.get("type") == "text" and not p.get("synthetic") and (p.get("text") or "").strip():
                texts.append(p["text"].strip())
            elif p.get("type") == "tool" and p.get("tool"):
                tools.append(describe_tool(p, directory))
            elif p.get("type") == "compaction":
                compaction = True
        if role not in ("user", "assistant"):
            continue
        if compaction:
            # Where opencode compacted the context: the model saw only the
            # summary below from here on; everything above is still kept.
            body.append("## Compaction\n\n_The context was compacted here; the messages above "
                        "were summarized for the model._")
        if not (texts or tools):
            continue
        block = "\n\n".join(texts)
        if tools:
            overflow = len(tools) - _TOOL_LINES
            lines = tools[:_TOOL_LINES] + ([f"- \u2026 and {overflow} more tool calls"] if overflow > 0 else [])
            block += ("\n\n" if block else "") + "Tools:\n" + "\n".join(lines)
        # opencode marks the compaction summary with summary: true (a user
        # message's summary is an object with its diffs).
        if role == "assistant" and info.get("summary") is True:
            heading = "Compaction summary"
        else:
            heading = "User" if role == "user" else "Assistant"
        body.append(f"## {heading}\n\n{block}")
    if not any(b.startswith("## User") for b in body):
        return None
    title = redact(title or "")
    head = ["---", "type: session", "source: harness", f"session: {sid}", f"title: {_yaml(title)}",
            f"directory: {_yaml(directory)}", f"created: {_iso(created)}", f"updated: {_iso(updated)}",
            f"tokens_input: {tin or 0}", f"tokens_output: {tout or 0}", *extra, "---", "",
            f"# {title or sid}", ""]
    return "\n".join(head) + redact("\n\n".join(body)) + "\n"


def context_refs(vault, files):
    """The vault paths naming each instruction file; a file from outside the
    vault is copied to .raw/context/<sha12>.md first."""
    refs, root = [], _norm_dir(os.path.abspath(vault))
    for f in files:
        try:
            with open(f, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        full = _norm_dir(os.path.abspath(f))
        if full.startswith(root + "/"):
            refs.append(full[len(root) + 1:])
            continue
        rel = f".raw/context/{hashlib.sha256(data).hexdigest()[:12]}.md"
        dest = os.path.join(vault, rel)
        if not os.path.exists(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            text = redact(data.decode("utf-8", "replace"))
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"---\ntype: context\ntitle: {_yaml('instructions from ' + os.path.basename(f))}\n"
                         f"path: {_yaml(f)}\n---\n\n{text}")
        refs.append(rel)
    return refs


def _old_lines(path, keys):
    """Front matter lines starting with one of `keys` in an existing file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return []
    end = head.find("\n---", 4)
    return [ln for ln in head[4:end].splitlines() if ln.split(":", 1)[0] in keys] if end > 0 else []


def main(argv):
    args, opts = [], {"--launch": None, "--ticket": "", "--context": []}
    it = iter(argv[1:])
    for a in it:
        if a in opts:
            v = next(it, "")
            if a == "--context":
                opts[a].append(v)
            else:
                opts[a] = v
        else:
            args.append(a)
    if len(args) != 4:
        print("usage: vault_capture.py <opencode.db> <vault dir> <since epoch ms> <project dir>"
              " [--launch MS] [--ticket KEY] [--context FILE]...", file=sys.stderr)
        return 0
    db, vault, since, project = args[0], args[1], int(args[2]), args[3]
    launch = int(opts["--launch"]) if (opts["--launch"] or "").isdigit() else None
    fresh = []
    if launch is not None:
        if re.match(r"^[A-Za-z][A-Za-z0-9_]*-[0-9]+$", opts["--ticket"]):
            fresh.append(f"ticket: {opts['--ticket']}")
        refs = context_refs(vault, opts["--context"]) if opts["--context"] else []
        if refs:
            fresh.append("context: [" + ", ".join(_yaml(r) for r in refs) + "]")
    if not os.path.isfile(db):
        return 0
    try:
        # as_uri percent-encodes spaces and the like (a Windows profile path).
        con = sqlite3.connect(pathlib.Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        sessions = con.execute(
            "SELECT id, title, directory, time_created, time_updated, tokens_input, tokens_output "
            "FROM session WHERE parent_id IS NULL AND time_updated >= ? ORDER BY time_created",
            (since,)).fetchall()
        out = os.path.join(vault, ".raw", "sessions")
        for sess in sessions:
            if not _in_dir(sess[2], project):
                continue
            day = datetime.datetime.fromtimestamp(sess[3] / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")
            path = os.path.join(out, f"{day}-{sess[0]}.md")
            # This launch's ticket and context for the sessions it touched;
            # the others keep what an earlier capture recorded.
            if launch is not None and sess[4] >= launch:
                extra = fresh
            else:
                extra = _old_lines(path, ("ticket", "context"))
            text = render(con, sess, extra)
            if text is None:
                continue
            os.makedirs(out, exist_ok=True)
            # Never shrink a captured session: if opencode lost messages (an
            # undo, a revert), the file keeps what it had.
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    old = f.read()
                if text.count("\n## ") < old.count("\n## "):
                    print(f"vault capture: {path} kept, the session now has fewer messages", file=sys.stderr)
                    continue
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            print(path)
    except (sqlite3.Error, OSError, ValueError) as e:
        print(f"vault capture skipped: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
