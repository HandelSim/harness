#!/usr/bin/env python3
"""Copy the opencode conversations of one harness launch into the active vault.

Usage: vault_capture.py <opencode.db> <vault dir> <since epoch ms> <project dir>

Reads opencode's SQLite store read-only and writes one markdown file per top
level session started in <project dir> (or below it) and updated since <since>
to <vault>/.raw/sessions/, named
<YYYY-MM-DD>-<session id>.md, so a rerun rewrites the same file. Only the
user's and the assistant's text is kept (no tool output, no reasoning, no
text opencode injected), with tool calls listed by name, and anything shaped
like a key or token is redacted. The database is shared by every opencode
session of the user (or of every harness container), so the directory filter
is what keeps other projects' conversations out of the vault. Prints the files it wrote. Never fails the
launch: any problem is one line on stderr and exit status 0.
"""
import datetime
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
    d, w = _norm_dir(directory), _norm_dir(want)
    return bool(w) and (d == w or d.startswith(w + "/"))


def _yaml(s):
    return json.dumps(str(s or ""), ensure_ascii=False)


def render(con, sess):
    """One session as markdown, or None when it has no conversation text."""
    sid, title, directory, created, updated, tin, tout = sess
    body = []
    msgs = con.execute("SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created, id",
                       (sid,)).fetchall()
    for mid, mdata in msgs:
        try:
            role = json.loads(mdata).get("role")
        except (TypeError, ValueError):
            continue
        texts, tools = [], []
        for (pdata,) in con.execute("SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id",
                                    (mid,)):
            try:
                p = json.loads(pdata)
            except (TypeError, ValueError):
                continue
            if p.get("type") == "text" and not p.get("synthetic") and (p.get("text") or "").strip():
                texts.append(p["text"].strip())
            elif p.get("type") == "tool" and p.get("tool"):
                tools.append(p["tool"])
        if role not in ("user", "assistant") or not (texts or tools):
            continue
        block = "\n\n".join(texts)
        if tools:
            counts = {}
            for t in tools:
                counts[t] = counts.get(t, 0) + 1
            block += ("\n\n" if block else "") + "_tools: " + ", ".join(
                f"{t} x{n}" if n > 1 else t for t, n in counts.items()) + "_"
        body.append(f"## {'User' if role == 'user' else 'Assistant'}\n\n{block}")
    if not any(b.startswith("## User") for b in body):
        return None
    title = redact(title or "")
    head = ["---", "type: session", "source: harness", f"session: {sid}", f"title: {_yaml(title)}",
            f"directory: {_yaml(directory)}", f"created: {_iso(created)}", f"updated: {_iso(updated)}",
            f"tokens_input: {tin or 0}", f"tokens_output: {tout or 0}", "---", "",
            f"# {title or sid}", ""]
    return "\n".join(head) + redact("\n\n".join(body)) + "\n"


def main(argv):
    if len(argv) != 5:
        print("usage: vault_capture.py <opencode.db> <vault dir> <since epoch ms> <project dir>",
              file=sys.stderr)
        return 0
    db, vault, since, project = argv[1], argv[2], int(argv[3]), argv[4]
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
            text = render(con, sess)
            if text is None:
                continue
            day = datetime.datetime.fromtimestamp(sess[3] / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")
            path = os.path.join(out, f"{day}-{sess[0]}.md")
            os.makedirs(out, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            print(path)
    except (sqlite3.Error, OSError, ValueError) as e:
        print(f"vault capture skipped: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
