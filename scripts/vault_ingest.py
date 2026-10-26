#!/usr/bin/env python3
"""Ingest sources into a harness vault, and keep the vault's generated pages up to date.

  vault_ingest.py ingest --vault V --proxy URL --model M [--src DIR] [--max N]
                         [--unit-kb K] [--retry-failed] [--watch SECONDS] [--dry-run] [--commit]
  vault_ingest.py index  --vault V

harness does the bookkeeping, the model only writes text. The sources are the
vault's .raw/ files (captured sessions included) and any codebase given with
--src (remembered, so a later run re-checks it). Each source is cut into units
of at most --unit-kb: a code directory's files (split when large), or a run of
whole messages of a session or document. A unit goes to the model as ONE plain
chat request through the harness proxy with header x-harness-raw: 1, so a free
model without tools gets no agent scaffold and answers in text. The reply is
checked (a summary line, a body, and the `names` it quotes must occur in the
source), retried once with the problems listed, and written as one section of
a page. The manifest .harness/ingest.json holds each unit's content hash, so a
run only sends units that are new or changed; a section of a page is replaced
without touching the others.

Pages: wiki/code/<repo>/<repo>--<dir with / as -->.md per directory, with the
root directory's files and a generated module tree in wiki/code/<repo>/<repo>.md;
wiki/sources/<path under .raw with / as -->.md per .raw file. `index` rebuilds
the generated parts: topic hubs (wiki/topics/, from the sections' Topics lines,
two or more pages), ticket hubs (wiki/tickets/, ids found with the charter's
ticket_pattern), the module trees and up/down links, and wiki/index.md.
Generated files carry `generated: harness` in their frontmatter; nothing else
is ever rewritten.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

UNIT_KB = 48
MAX_FILE_BYTES = 1_000_000
INFRA_STOP = 3            # consecutive proxy failures that end a run
COMMIT_EVERY = 10         # units between commits
REPLY_CAP = 12_000        # chars of a section kept
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", "__pycache__", ".venv",
             "venv", ".tox", ".mypy_cache", ".pytest_cache", ".next", ".cache", "coverage", ".idea",
             ".vscode", "third_party", ".gradle", "bin", "obj"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg", ".pdf", ".zip", ".gz",
            ".tgz", ".bz2", ".xz", ".7z", ".tar", ".jar", ".war", ".class", ".so", ".dll", ".dylib",
            ".exe", ".o", ".a", ".pyc", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
            ".mov", ".avi", ".wav", ".db", ".sqlite", ".bin", ".map", ".lock", ".min.js", ".min.css"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "poetry.lock",
              "go.sum", "composer.lock", "Gemfile.lock", ".DS_Store"}
GENERATED = "generated: harness"
BAD_NAME = re.compile(r'[\[\]#^|\\:*?"<>/]')


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def read_text(path, limit=None):
    try:
        with open(path, "rb") as f:
            data = f.read() if limit is None else f.read(limit)
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_if_changed(path, text):
    old = read_text(path)
    if old != text:
        write_atomic(path, text)
        return True
    return False


def frontmatter(text):
    """(dict of top-level `key: value` lines, body) of a markdown file."""
    if not text or not text.startswith("---\n"):
        return {}, text or ""
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        m = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = unquote(m.group(2).strip())
    return meta, text[end + 5:]


def unquote(v):
    """A one-line YAML scalar's value: 'a''b' and "a\\"b" forms included."""
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    return v


def yq(value):
    """A YAML-safe one-line scalar."""
    value = re.sub(r"\s+", " ", str(value)).strip()
    return json.dumps(value) if re.search(r'[:#\[\]{}&*!|>\'"%@`,]|^[-?]', value) or not value else value


def safe_name(text):
    return BAD_NAME.sub("_", text).strip(" .") or "_"


# --- charter ---------------------------------------------------------------

def charter(vault):
    meta, body = frontmatter(read_text(os.path.join(vault, "wiki", "overview.md")) or "")
    pattern = meta.get("ticket_pattern", "").strip()
    try:
        ticket_re = re.compile(r"\b(?:%s)\b" % pattern) if pattern else None
    except re.error:
        ticket_re = None
    lines = []
    for key in ("project", "goal", "readers", "track"):
        if meta.get(key):
            lines.append(f"{key}: {meta[key]}")
    text = "\n".join(lines)
    extra = body.strip()
    if extra:
        text += ("\n" if text else "") + extra[:1200]
    return text, ticket_re


# --- manifest --------------------------------------------------------------

def manifest_path(vault):
    return os.path.join(vault, ".harness", "ingest.json")


def load_manifest(vault):
    try:
        with open(manifest_path(vault), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = {}
    m.setdefault("version", 1)
    m.setdefault("repos", {})
    m.setdefault("units", {})
    return m


def save_manifest(vault, m):
    write_atomic(manifest_path(vault), json.dumps(m, indent=1, sort_keys=True) + "\n")


# --- units -----------------------------------------------------------------

def repo_files(root):
    """Text files of a codebase, relative paths, git-aware."""
    rels = []
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, timeout=60, check=True).stdout
        rels = [p for p in out.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
            for name in files:
                rels.append(os.path.relpath(os.path.join(d, name), root).replace(os.sep, "/"))
    keep = []
    for rel in sorted(set(rels)):
        parts = rel.split("/")
        name = parts[-1]
        low = name.lower()
        if any(p in SKIP_DIRS for p in parts[:-1]) or name in SKIP_NAMES:
            continue
        if any(low.endswith(e) for e in SKIP_EXT):
            continue
        path = os.path.join(root, rel)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        keep.append(rel)
    return keep


def pack(pieces, budget):
    """Greedy packing of (label, text) pieces into chunks of at most `budget`
    chars; a piece bigger than the budget is cut by lines. Appending pieces
    leaves the earlier chunks unchanged."""
    chunks, cur, size = [], [], 0
    for label, text in pieces:
        if len(text) > budget:
            if cur:
                chunks.append(cur)
                cur, size = [], 0
            lines = text.splitlines(keepends=True)
            start, buf, n = 1, [], 0
            for i, line in enumerate(lines, 1):
                if buf and n + len(line) > budget:
                    chunks.append([(f"{label} lines {start}-{i - 1}", "".join(buf))])
                    start, buf, n = i, [], 0
                buf.append(line[:budget])
                n += len(line[:budget])
            if buf:
                chunks.append([(f"{label} lines {start}-{len(lines)}", "".join(buf))])
            continue
        if cur and size + len(text) > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur.append((label, text))
        size += len(text)
    if cur:
        chunks.append(cur)
    return chunks


def code_units(repo, root, budget):
    by_dir = {}
    for rel in repo_files(root):
        by_dir.setdefault(os.path.dirname(rel), []).append(rel)
    units = []
    for d, files in by_dir.items():
        pieces = []
        for rel in files:
            text = read_text(os.path.join(root, rel))
            if text is None or not text.strip():
                continue
            lines = text.splitlines()
            if lines and sum(len(x) for x in lines) / len(lines) > 400:
                continue  # minified or data
            pieces.append((rel, text))
        if not pieces:
            continue
        page = repo if not d else f"{repo}--{'--'.join(safe_name(p) for p in d.split('/'))}"
        chunks = pack(pieces, budget)
        for i, chunk in enumerate(chunks, 1):
            text = "".join(f"=== {label} ===\n{body}\n" for label, body in chunk)
            units.append({
                "id": f"code:{repo}/{d or '.'}#{i}", "kind": "code", "page": page,
                "dir": f"wiki/code/{safe_name(repo)}", "repo": repo, "path": d or ".",
                "part": i, "parts": len(chunks), "labels": [label for label, _ in chunk],
                "files": files, "text": text, "sha": sha(text), "depth": d.count("/") + (1 if d else 0),
            })
    units.sort(key=lambda u: (-u["depth"], u["path"], u["part"]))
    return units


def raw_units(vault, budget):
    units = []
    raw = os.path.join(vault, ".raw")
    for d, dirs, files in os.walk(raw):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or any(name.lower().endswith(e) for e in SKIP_EXT):
                continue
            path = os.path.join(d, name)
            rel = os.path.relpath(path, raw).replace(os.sep, "/")
            text = read_text(path, MAX_FILE_BYTES * 4)
            if text is None or not text.strip():
                continue
            meta, body = frontmatter(text)
            # Messages (## headings) are the cut points: a session that grows
            # only changes its last unit.
            pieces, cur = [], []
            for line in body.splitlines(keepends=True):
                if line.startswith("## ") and cur:
                    pieces.append(("", "".join(cur)))
                    cur = []
                cur.append(line)
            if cur:
                pieces.append(("", "".join(cur)))
            chunks = pack(pieces, budget)
            stem = re.sub(r"\.[A-Za-z0-9]+$", "", rel)
            page = "--".join(safe_name(p) for p in stem.split("/"))
            title = meta.get("title", "") or stem
            for i, chunk in enumerate(chunks, 1):
                piece = "".join(t for _, t in chunk)
                units.append({
                    "id": f"raw:{rel}#{i}", "kind": "raw", "page": page, "dir": "wiki/sources",
                    "source": f".raw/{rel}", "title": title, "stype": meta.get("type", "document"),
                    "part": i, "parts": len(chunks), "labels": [], "files": [],
                    "text": piece, "sha": sha(piece), "depth": 0,
                })
    return units


def plan(vault, m, srcs, budget):
    units = raw_units(vault, budget)
    for name, root in sorted(m["repos"].items()):
        if os.path.isdir(root):
            units += code_units(name, root, budget)
        elif name in srcs:
            print(f"[ingest] {name}: {root} not found, skipped", file=sys.stderr)
    return units


def pending(units, m, retry_failed):
    out = []
    for u in units:
        e = m["units"].get(u["id"])
        if e is None or e.get("sha") != u["sha"]:
            out.append(u)
        elif e.get("status") == "failed" and retry_failed:
            out.append(u)
    return out


# --- the model call ----------------------------------------------------------

CODE_FORMAT = """summary: <one sentence, at most 25 words: what this code is for>
### Purpose
<2 to 5 sentences: what this part of the codebase does and why it exists>
### Key files
- `<file path exactly as in the source>`: <its role>; key names: `<name>`, `<name>`
### Connections
<what it calls, imports or is used by, with exact `names`; "none seen" if none>
### Gotchas
<non-obvious behavior, constraints, TODOs or risks seen in the code; "none seen" if none>
Topics: [[<topic>]], [[<topic>]]"""

RAW_FORMAT = """summary: <one sentence, at most 25 words: what this part is about>
### What happened
<the questions asked and the work done, in order, 3 to 8 sentences>
### Decisions
- <a decision and its reason>
### Facts
- <a durable fact worth finding later: exact `paths`, `commands`, `names`, values>
### Open questions
- <unfinished work or open questions; "none" if none>
Topics: [[<topic>]], [[<topic>]]"""


def build_prompt(u, charter_text, context):
    if u["kind"] == "code":
        what = (f"directory `{u['path']}` of the codebase `{u['repo']}`"
                + (f" (part {u['part']} of {u['parts']})" if u["parts"] > 1 else ""))
        fmt = CODE_FORMAT
    else:
        what = (f"the {u['stype']} `{u['source']}`" + (f", part {u['part']} of {u['parts']}" if u["parts"] > 1 else ""))
        fmt = RAW_FORMAT
    rules = f"""Write ONE section of a knowledge-base page about {what}.
Reply with ONLY the section, in exactly this format:

{fmt}

Rules:
- Start with the `summary:` line. No preamble, no closing remarks, no tool calls, no JSON.
- Only state what the source shows. Wrap file paths, functions, classes, commands and config keys
  in backticks, spelled exactly as in the source; never invent a name that is not in it.
- Topics: 1 to 5 short lowercase noun phrases naming the concepts this part is about
  (for example [[authentication]], [[retry logic]]), each as a [[wikilink]].
- Be dense and specific: this section is what someone searching the vault will read instead of the source.
- Code examples: at most 5 lines each."""
    parts = []
    if charter_text:
        parts.append("<<<BEGIN_VAULT_CHARTER>>>\n" + charter_text + "\n<<<END_VAULT_CHARTER>>>")
    if context:
        parts.append("<<<BEGIN_CONTEXT>>>\n" + context + "\n<<<END_CONTEXT>>>")
    parts.append("<<<BEGIN_SOURCE>>>\n" + u["text"] + "\n<<<END_SOURCE>>>")
    # The rules come last on purpose: a weak model follows what it read most recently.
    return rules + "\n\n" + "\n\n".join(parts) + "\n\n" + "Reminder, the task: " + rules


class InfraError(Exception):
    pass


def call_model(proxy, model, messages, timeout):
    body = json.dumps({"model": model, "messages": messages, "stream": False}).encode()
    req = urllib.request.Request(proxy.rstrip("/") + "/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "x-harness-raw": "1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise InfraError(f"HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise InfraError(str(e)) from None
    try:
        return data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        raise InfraError(f"unexpected reply: {str(data)[:200]}") from None


def topic_name(raw):
    t = re.sub(r"\s+", " ", raw.replace("_", " ")).strip().lower()
    t = safe_name(t)
    return t if 2 <= len(t) <= 60 else ""


def clean_reply(text):
    """(summary, body, topics) from a model reply."""
    text = text.replace("\r\n", "\n").strip()
    fence = re.match(r"^```[\w-]*\n(.*)\n```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"\s*\**summary\**\s*:", ln, re.I)), None)
    if start is None:
        return "", text, []
    summary = re.sub(r"^\s*\**summary\**\s*:\s*", "", lines[start], flags=re.I).strip().strip("*").strip()
    body, topics = [], []
    for ln in lines[start + 1:]:
        m = re.match(r"\s*\**topics\**\s*:\s*(.*)$", ln, re.I)
        if m:
            found = re.findall(r"\[\[([^\]|#]+)", m.group(1)) or m.group(1).split(",")
            for t in found:
                t = topic_name(t)
                if t and t not in topics:
                    topics.append(t)
            continue
        # Headings nest under the page's own: anything bigger than ### becomes ###.
        ln = re.sub(r"^#{1,2}(?=\s)", "###", ln)
        body.append(ln)
    body = "\n".join(body).strip()
    if len(body) > REPLY_CAP:
        body = body[:REPLY_CAP].rsplit("\n", 1)[0] + "\n\n_(truncated)_"
    return summary[:300], body, topics[:5]


def grounding(body, summary, source):
    """Backticked names in the reply that do not occur in the source."""
    unknown, names = [], 0
    for tok in re.findall(r"`([^`\n]{2,100})`", summary + "\n" + body):
        tok = tok.strip()
        if " " in tok or not re.search(r"[A-Za-z]", tok):
            continue
        names += 1
        cands = {tok, tok.rstrip("()"), tok.strip("./"), tok.split("(")[0], tok.rsplit("/", 1)[-1]}
        if not any(c and c in source for c in cands):
            unknown.append(tok)
    return unknown, names


def check(reply, u):
    summary, body, topics = clean_reply(reply)
    problems = []
    if not summary:
        problems.append("the reply must start with a `summary:` line")
    if len(body) < 40:
        problems.append("the section body is missing or nearly empty")
    if re.search(r'```json\s*\{\s*"name"', reply):
        problems.append("no tool calls or JSON: reply with the markdown section only")
    unknown, names = grounding(body, summary, u["text"])
    if len(unknown) > 2 and len(unknown) * 5 > names:
        problems.append("these names are not in the source, so they are invented: "
                        + ", ".join(f"`{t}`" for t in unknown[:12])
                        + ". Use only names that appear in the source, spelled exactly")
    return summary, body, topics, problems


def ingest_unit(u, proxy, model, charter_text, context, timeout):
    """(summary, body, topics, problems); raises InfraError."""
    prompt = build_prompt(u, charter_text, context)
    messages = [{"role": "user", "content": prompt}]
    reply = call_model(proxy, model, messages, timeout)
    summary, body, topics, problems = check(reply, u)
    if not problems:
        return summary, body, topics, []
    messages += [{"role": "assistant", "content": reply},
                 {"role": "user", "content": "That section has problems:\n- " + "\n- ".join(problems)
                  + "\nWrite the whole section again, fixed, in the same format, starting with `summary:`."}]
    reply = call_model(proxy, model, messages, timeout)
    return check(reply, u)


# --- pages ---------------------------------------------------------------------

UNIT_RE = re.compile(r"<!-- unit:(\S+) sha:(\w+) -->\n(.*?)\n<!-- /unit -->", re.S)
BLOCK_RE = re.compile(r"\n?<!-- links -->.*?<!-- /links -->\n?", re.S)


def page_path(vault, u):
    return os.path.join(vault, u["dir"], u["page"] + ".md")


def existing_sections(path):
    return {m.group(1): (m.group(2), m.group(3)) for m in UNIT_RE.finditer(read_text(path) or "")}


def section_summary(text):
    m = re.search(r"^> (.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def render_section(u, summary, body, topics, tickets):
    head = ""
    if u["parts"] > 1:
        label = ", ".join(f"`{x}`" for x in u["labels"][:6]) if u["labels"] else ""
        head = f"## Part {u['part']}" + (f": {label}" + (" …" if len(u["labels"]) > 6 else "") if label else "") + "\n"
    out = head + f"> {summary}\n\n{body}\n"
    if topics:
        out += "\nTopics: " + ", ".join(f"[[{t}]]" for t in topics) + "\n"
    if tickets:
        out += "Tickets: " + ", ".join(f"[[{t}]]" for t in tickets) + "\n"
    return out.rstrip("\n")


def assemble_page(vault, page_units, new):
    """Write a page from its current units: kept sections, the new ones in
    `new` ({unit id: section text}) and placeholders for the rest."""
    u0 = page_units[0]
    path = page_path(vault, u0)
    old_text = read_text(path) or ""
    old = existing_sections(path)
    links = BLOCK_RE.search(old_text)
    sections, summary = [], ""
    for u in page_units:
        if u["id"] in new:
            sec_sha, sec = u["sha"], new[u["id"]]
        elif u["id"] in old:
            sec_sha, sec = old[u["id"]]
        else:
            sec_sha, sec = "pending", (f"## Part {u['part']}\n" if u["parts"] > 1 else "") + "_Not ingested yet._"
        summary = summary or section_summary(sec)
        sections.append(f"<!-- unit:{u['id']} sha:{sec_sha} -->\n{sec}\n<!-- /unit -->")
    if u0["kind"] == "code":
        title = u0["repo"] if u0["path"] == "." else f"{u0['repo']}/{u0['path']}"
        meta = ["type: code", f"repo: {yq(u0['repo'])}", f"path: {yq(u0['path'])}"]
        files = u0["files"]
        intro = (f"Generated by `harness ingest` from `{title}`. Edits here are overwritten: "
                 "put your own notes in another page and link to this one.\n\n## Files\n"
                 + ", ".join(f"`{f.rsplit('/', 1)[-1]}`" for f in files[:60])
                 + (f" and {len(files) - 60} more" if len(files) > 60 else "") + "\n")
    else:
        title = u0["title"]
        meta = [f"type: {'session' if u0['stype'] == 'session' else 'source'}",
                f"source: {yq(u0['source'])}", f"aliases: [{yq(u0['title'])}]"]
        intro = (f"Generated by `harness ingest` from `{u0['source']}` (the full text is there). "
                 "Edits here are overwritten: put your own notes in another page and link to this one.\n")
    text = ("---\n" + "\n".join(meta) + f"\nsummary: {yq(summary or 'not ingested yet')}\n"
            f"updated: {now_iso()[:10]}\n{GENERATED}\n---\n\n# {title}\n\n{intro}\n"
            + "\n\n".join(sections) + "\n" + (links.group(0) if links else ""))
    write_atomic(path, text.rstrip("\n") + "\n")


def context_for(vault, u, sections_by_page):
    """Summaries the model sees beside the source: the page's other parts and,
    for code, the subdirectory pages already written."""
    lines = []
    for other_id, summ in sections_by_page.get(u["page"], []):
        if other_id != u["id"] and summ:
            lines.append(f"- other part of this page: {summ}")
    if u["kind"] == "code":
        base = os.path.join(vault, u["dir"])
        prefix = u["page"] + "--" if u["path"] != "." else u["repo"] + "--"
        try:
            names = sorted(n for n in os.listdir(base) if n.startswith(prefix) and n.endswith(".md"))
        except OSError:
            names = []
        for n in names:
            rest = n[len(prefix):-3]
            if "--" in rest:
                continue  # only direct children
            meta, _ = frontmatter(read_text(os.path.join(base, n), 4096) or "")
            lines.append(f"- subdirectory [[{n[:-3]}]]: {meta.get('summary', '')}")
    return "\n".join(lines[:40])


# --- generated hubs and index ------------------------------------------------------

def wiki_pages(vault):
    root = os.path.join(vault, "wiki")
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for n in sorted(files):
            if n.endswith(".md"):
                p = os.path.join(d, n)
                yield p, os.path.relpath(p, root).replace(os.sep, "/")


def regenerate(vault):
    """Rebuild every generated hub, link block and wiki/index.md. Cheap and
    deterministic: run after ingest and after every session."""
    pages = {}
    for path, rel in wiki_pages(vault):
        text = read_text(path) or ""
        meta, body = frontmatter(text)
        pages[rel] = {"path": path, "name": rel.rsplit("/", 1)[-1][:-3], "meta": meta, "body": body,
                      "generated": GENERATED in text[:2000]}
    hubs = lambda rel: rel.startswith(("topics/", "tickets/"))  # noqa: E731
    topics, tickets = {}, {}
    for rel, p in pages.items():
        if hubs(rel) or rel in ("index.md", "log.md"):
            continue
        for line in re.findall(r"^Topics:(.*)$", p["body"], re.M):
            for t in re.findall(r"\[\[([^\]|#]+)", line):
                topics.setdefault(t.strip(), set()).add(rel)
        for line in re.findall(r"^Tickets:(.*)$", p["body"], re.M):
            for t in re.findall(r"\[\[([^\]|#]+)", line):
                tickets.setdefault(t.strip(), set()).add(rel)

    def summ(rel):
        return pages[rel]["meta"].get("summary", "") if rel in pages else ""

    def hub(folder, kind, name, refs, intro, more=(), summary=None):
        rel = f"{folder}/{name}.md"
        lines = [f"- [[{pages[r]['name']}]]: {summ(r)}" for r in sorted(refs)]
        summary = summary or f"{len(refs)} pages about {name}"
        text = (f"---\ntype: {kind}\nsummary: {yq(summary)}\n{GENERATED}\n---\n\n"
                f"# {name}\n\n{intro}\n\n" + "\n".join(lines) + "\n" + "".join(more))
        write_if_changed(os.path.join(vault, "wiki", rel), text)
        return rel

    # Tickets worked on with `harness --ticket`: the tracker's copy in
    # .raw/tickets/ and the sessions whose front matter names the ticket.
    def source(raw_rel, title):
        """A .raw file as a link to its source page once ingested, else its path."""
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", raw_rel)
        page = "--".join(safe_name(p) for p in stem.split("/"))
        link = f"[[{page}]]" if f"sources/{page}.md" in pages else f"`.raw/{raw_rel}`"
        return f"- {link}" + (f": {title}" if title else "") + "\n"

    worked = {}
    for sub in ("tickets", "sessions"):
        d = os.path.join(vault, ".raw", sub)
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not n.endswith(".md"):
                continue
            meta, _ = frontmatter(read_text(os.path.join(d, n), 4000) or "")
            t = meta.get("ticket", "").strip()
            if t and BAD_NAME.search(t) is None:
                w = worked.setdefault(t, {"tickets": [], "sessions": []})
                w[sub].append(source(f"{sub}/{n}", meta.get("title", "")))
    keep = set()
    for t, refs in topics.items():
        if len(refs) >= 2:
            keep.add(hub("topics", "topic", t, refs, "Generated topic hub: every page whose Topics line names it."))
    for t in sorted(set(tickets) | set(worked)):
        w = worked.get(t, {})
        more = []
        if w.get("tickets"):
            more += ["\n## The ticket\n\nAs written in the tracker, refreshed at each launch:\n\n"] + w["tickets"]
        if os.path.isfile(os.path.join(vault, "wiki", "notes", "tickets", f"{t}-state.md")):
            more += [f"\nState kept by the agents: [[{t}-state]]\n"]
        if w.get("sessions"):
            more += [f"\n## Sessions ({len(w['sessions'])})\n\n"] + w["sessions"]
        n = len(tickets.get(t, ()))
        summary = f"{n} pages about {t}" + (f", {len(w['sessions'])} sessions on it" if w.get("sessions") else "")
        keep.add(hub("tickets", "ticket", t, tickets.get(t, set()),
                     "Generated ticket hub: every page that mentions this ticket.", more, summary))
    for rel, p in list(pages.items()):
        if hubs(rel) and p["generated"] and rel not in keep:
            os.remove(p["path"])
            del pages[rel]

    # Up/down links of code pages and each repo's module tree. A repo whose
    # root directory has no files still gets its overview page.
    code_root = os.path.join(vault, "wiki", "code")
    for repo in sorted(os.listdir(code_root)) if os.path.isdir(code_root) else []:
        rel = f"code/{repo}/{repo}.md"
        if rel not in pages and os.path.isdir(os.path.join(code_root, repo)):
            path = os.path.join(code_root, repo, repo + ".md")
            text = (f"---\ntype: code\nrepo: {yq(repo)}\npath: .\nsummary: {yq(f'overview of the {repo} codebase')}\n"
                    f"{GENERATED}\n---\n\n# {repo}\n\nGenerated by `harness ingest`: the module tree of `{repo}`.\n")
            write_atomic(path, text)
            meta, body = frontmatter(text)
            pages[rel] = {"path": path, "name": repo, "meta": meta, "body": body, "generated": True}
    code = {rel: p for rel, p in pages.items() if rel.startswith("code/") and p["generated"]
            and p["meta"].get("type") == "code"}
    by_name = {p["name"]: rel for rel, p in code.items()}
    for rel, p in code.items():
        name, repo = p["name"], p["meta"].get("repo", "")
        children = sorted(n for n in by_name if n.startswith(name + "--") and "--" not in n[len(name) + 2:]) \
            if name != repo else sorted(n for n in by_name if n.startswith(repo + "--")
                                        and "--" not in n[len(repo) + 2:])
        block = []
        if children:
            block.append("## Subdirectories\n" + "\n".join(f"- [[{c}]]: {summ(by_name[c])}" for c in children))
        if name != repo:
            parent = name.rsplit("--", 1)[0]
            block.append(f"Up: [[{parent if parent in by_name or parent == repo else repo}]]")
        if name == repo:
            tree = sorted(n for n in by_name if n.startswith(repo + "--"))
            block.append(f"## Module tree ({len(tree)} directory pages)\n" + "\n".join(
                f"{'  ' * (n.count('--') - 1)}- [[{n}]]: {summ(by_name[n])}" for n in tree))
        text = read_text(p["path"]) or ""
        text = BLOCK_RE.sub("\n", text).rstrip("\n") + "\n"
        if block:
            text += "\n<!-- links -->\n" + "\n\n".join(block) + "\n<!-- /links -->\n"
        write_if_changed(p["path"], text)

    # wiki/index.md: one line per page outside code/ (each repo is one line),
    # grouped by folder. Agents grep it; nobody edits it.
    groups = {}
    for rel, p in sorted(pages.items()):
        if rel in ("index.md", "log.md"):
            continue
        folder = rel.split("/")[0] if "/" in rel else "."
        if folder == "code":
            parts = rel.split("/")
            if len(parts) != 3 or p["name"] != parts[1]:
                continue
            n = sum(1 for r in code if r.startswith(f"code/{parts[1]}/") and r != rel)
            line = f"- [[{p['name']}]]: {summ(rel)} ({n} directory pages)"
        else:
            line = f"- [[{p['name']}]]: {summ(rel) or '(no summary)'}"
        groups.setdefault(folder, []).append(line)
    order = [".", "notes", "code", "sources", "topics", "tickets"]
    heads = {".": "Top level", "notes": "Notes", "code": "Codebases", "sources": "Sources and sessions",
             "topics": "Topics", "tickets": "Tickets"}
    out = ["---", "type: index", "summary: every page of this vault, one line each", GENERATED, "---", "",
           "# Index", "",
           "Generated by harness; do not edit. Find a page: grep this file for a word, or",
           "`grep -ril <word> wiki/` and then read the `summary:` lines of the hits.",
           "A codebase's directory pages are listed in its overview page's module tree.", ""]
    for folder in order + sorted(f for f in groups if f not in order):
        if folder in groups:
            out += [f"## {heads.get(folder, folder)}", ""] + groups[folder] + [""]
    write_if_changed(os.path.join(vault, "wiki", "index.md"), "\n".join(out))


def log(vault, line):
    path = os.path.join(vault, "wiki", "log.md")
    text = read_text(path)
    if text is None:
        text = "# Log\n\nAppend-only record of what was added to the vault.\n\n"
    write_atomic(path, text.rstrip("\n") + "\n" + line + "\n")


# --- git -------------------------------------------------------------------------

def git(vault, *args, timeout=60):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(["git", "-C", vault, *args], capture_output=True, timeout=timeout,
                              env=env).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def commit(vault, message):
    if not os.path.isdir(os.path.join(vault, ".git")):
        return
    git(vault, "add", "-A")
    if git(vault, "diff", "--cached", "--quiet"):
        return
    if not git(vault, "commit", "-q", "-m", message):
        print("[ingest] git commit failed (is git user.name/user.email set?)", file=sys.stderr)
        return
    if not git(vault, "rev-parse", "-q", "--verify", "@{u}"):
        return
    if git(vault, "push", "-q", timeout=120) or (git(vault, "pull", "-q", "--rebase", timeout=120)
                                                  and git(vault, "push", "-q", timeout=120)):
        return
    git(vault, "rebase", "--abort")
    print("[ingest] push failed; the commits stay local and go up with the next push", file=sys.stderr)


# --- commands ----------------------------------------------------------------------

def resolve_model(proxy, model):
    """A -withtools model's tool-less twin when the proxy lists one: ingest
    sends no tools, so the free cooperative model does the same work."""
    if not model.endswith("-withtools"):
        return model
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(proxy.rstrip("/") + "/models", timeout=20) as resp:
            ids = {m.get("id") for m in json.loads(resp.read()).get("data", [])}
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return model
    base = model[: -len("-withtools")]
    return base if base in ids else model


def run_ingest(a):
    vault = os.path.abspath(a.vault)
    m = load_manifest(vault)
    srcs = set()
    for src in a.src or []:
        root = os.path.abspath(src)
        if not os.path.isdir(root):
            print(f"[ingest] not a directory: {src}", file=sys.stderr)
            return 1
        try:
            common = os.path.commonpath([root, vault])
        except ValueError:  # different drives
            common = ""
        if common in (root, vault):
            print(f"[ingest] {src} contains or is inside the vault; ingest the codebase only", file=sys.stderr)
            return 1
        name = safe_name(os.path.basename(root.rstrip("/\\")))
        if m["repos"].get(name, root) != root:
            print(f"[ingest] the vault already has a codebase named {name} at {m['repos'][name]}", file=sys.stderr)
            return 1
        m["repos"][name] = root
        srcs.add(name)
    budget = max(4, a.unit_kb) * 1024
    charter_text, ticket_re = charter(vault)
    model = a.model if a.dry_run else resolve_model(a.proxy, a.model)
    done = failed = 0
    while True:
        units = plan(vault, m, srcs, budget)
        todo = pending(units, m, a.retry_failed)
        if a.max:
            todo = todo[: max(0, a.max - done - failed)]
        if a.dry_run:
            kb = sum(len(u["text"]) for u in todo) // 1024
            print(f"[ingest] {len(units)} units, {len(todo)} to send (~{kb} KB of source, "
                  f"~{kb * 1024 // 4 // 1000}k input tokens), {len({u['page'] for u in todo})} pages")
            for u in todo[:30]:
                print(f"  {u['id']}  ->  {u['dir']}/{u['page']}.md")
            if len(todo) > 30:
                print(f"  ... and {len(todo) - 30} more")
            return 0
        if srcs or not os.path.exists(manifest_path(vault)):
            save_manifest(vault, m)
        if todo:
            print(f"[ingest] {len(todo)} of {len(units)} units to ingest with {model}", file=sys.stderr)
        by_page = {}
        for u in units:
            by_page.setdefault(u["page"], []).append(u)
        infra, since_commit, wrote = 0, 0, 0
        for u in todo:
            path = page_path(vault, u)
            old = existing_sections(path)
            ctx = context_for(vault, u, {u["page"]: [(k, section_summary(v[1])) for k, v in old.items()]})
            try:
                summary, body, topics, problems = ingest_unit(u, a.proxy, model, charter_text, ctx, a.timeout)
            except InfraError as e:
                infra += 1
                print(f"[ingest] {u['id']}: proxy error ({e})", file=sys.stderr)
                if infra >= INFRA_STOP:
                    print(f"[ingest] {INFRA_STOP} proxy errors in a row; stopping (rerun to resume)", file=sys.stderr)
                    break
                time.sleep(min(60, 5 * infra))
                continue
            infra = 0
            entry = m["units"].get(u["id"], {})
            if problems:
                failed += 1
                entry = {"sha": u["sha"], "page": f"{u['dir']}/{u['page']}.md", "status": "failed",
                         "attempts": entry.get("attempts", 0) + 1, "problems": problems[:3], "at": now_iso()}
                print(f"[ingest] {u['id']}: failed: {'; '.join(problems)}", file=sys.stderr)
            else:
                done += 1
                tickets = sorted(set(ticket_re.findall(u["text"])))[:20] if ticket_re else []
                sec = render_section(u, summary, body, topics, tickets)
                assemble_page(vault, by_page[u["page"]], {u["id"]: sec})
                entry = {"sha": u["sha"], "page": f"{u['dir']}/{u['page']}.md", "status": "ok", "attempts": 0, "at": now_iso()}
                print(f"[ingest] {u['id']} -> {u['dir']}/{u['page']}.md", file=sys.stderr)
            m["units"][u["id"]] = entry
            save_manifest(vault, m)
            since_commit += 1
            wrote += 1
            if a.commit and since_commit >= COMMIT_EVERY:
                regenerate(vault)
                commit(vault, f"harness: ingest {since_commit} units")
                since_commit = 0
        # Units that no longer exist (a deleted file or directory) leave the manifest;
        # their pages are rebuilt from what is left.
        live = {u["id"] for u in units}
        gone = [k for k in m["units"] if k not in live]
        live_pages = {f"{u['dir']}/{u['page']}.md" for u in units}
        for k in gone:
            # A page whose source is gone entirely goes too (git keeps it).
            page = m["units"].pop(k).get("page", "")
            path = os.path.join(vault, page)
            if page.endswith(".md") and page not in live_pages and GENERATED in (read_text(path) or "")[:2000]:
                os.remove(path)
        for page_units in by_page.values():
            path = page_path(vault, page_units[0])
            if os.path.exists(path) and set(existing_sections(path)) - {u["id"] for u in page_units}:
                assemble_page(vault, page_units, {})
        if gone:
            save_manifest(vault, m)
        if wrote:
            log(vault, f"- {now_iso()} ingest: {done} sections written, {failed} failed ({model})")
        regenerate(vault)
        if a.commit:
            commit(vault, f"harness: ingest ({done} written, {failed} failed)")
        if infra >= INFRA_STOP:
            return 2
        if not a.watch or (a.max and done + failed >= a.max):
            break
        if not todo:
            time.sleep(a.watch)
    left = len(pending(plan(vault, m, srcs, budget), m, False))
    print(f"[ingest] done: {done} sections written, {failed} failed, {left} units left", file=sys.stderr)
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="vault_ingest.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("--vault", required=True)
    i.add_argument("--proxy", default="http://127.0.0.1:8000/v1")
    i.add_argument("--model", default="")
    i.add_argument("--src", action="append")
    i.add_argument("--max", type=int, default=0)
    i.add_argument("--unit-kb", type=int, default=UNIT_KB)
    i.add_argument("--timeout", type=int, default=300)
    i.add_argument("--retry-failed", action="store_true")
    i.add_argument("--watch", type=int, default=0)
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--commit", action="store_true")
    x = sub.add_parser("index")
    x.add_argument("--vault", required=True)
    a = p.parse_args(argv[1:])
    if a.cmd == "index":
        if os.path.isdir(os.path.join(a.vault, "wiki")):
            regenerate(os.path.abspath(a.vault))
        return 0
    vault = os.path.abspath(a.vault)
    # The lock lives in .git so it is never committed.
    lock_dir = os.path.join(vault, ".git") if os.path.isdir(os.path.join(vault, ".git")) else vault
    lock = open(os.path.join(lock_dir, "harness-ingest.lock"), "w")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass  # Windows: no lock
    except OSError:
        print("[ingest] another ingest is running on this vault", file=sys.stderr)
        return 1
    try:
        return run_ingest(a)
    except KeyboardInterrupt:
        print("\n[ingest] interrupted; progress is saved, rerun to resume", file=sys.stderr)
        if a.commit and not a.dry_run:
            regenerate(vault)
            commit(vault, "harness: ingest (interrupted)")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv))
