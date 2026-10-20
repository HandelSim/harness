"""SWE-bench Verified task environments without docker: dataset cache, per-task
checkout + venv, the gold-patch gate, and the grader (the same eval run on the
agent's diff).

Semantics are transcribed from SWE-bench v4.1.0 (swebench/harness/constants,
test_spec/python.py, log_parsers/python.py, grading.py) for the five repos the
probe uses; nothing is imported from swebench. Differences from the official
docker images are recorded per task in its build notes (python substitution,
skipped apt/locale steps) and the gold gate is the arbiter: a task whose env
does not reproduce FAIL -> PASS with the gold patch is excluded before any
token is spent.

Network: git clones from github.com, the HF datasets-server rows API, and pip
installs from PyPI while building envs. Never the model upstream.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

DATASET = "princeton-nlp/SWE-bench_Verified"
ROWS_URL = ("https://datasets-server.huggingface.co/rows?dataset=princeton-nlp%2FSWE-bench_Verified"
            "&config=default&split=test&offset={offset}&length=100")
IS_WINDOWS = os.name == "nt"
# Resolved via PATH: a bare "bash" on Windows can hit System32's WSL launcher
# before Git Bash.
BASH = shutil.which("bash") or "bash"
# Windows has no nice; children start at below-normal priority instead.
NICE = [] if IS_WINDOWS else ["nice", "-n", "19"] + (["ionice", "-c", "3"] if shutil.which("ionice") else [])

# runtests.py and bin/test run through the venv python rather than their
# `#!/usr/bin/env python` shebang: the same interpreter, and Windows cannot exec one.
TEST_DJANGO = "python tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1"
TEST_PYTEST = "pytest -rA"
TEST_SPHINX = "tox --current-env -epy39 -v --"
TEST_SYMPY = "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' python bin/test -C --verbose"
NON_TEST_EXTS = (".json", ".png", "csv", ".txt", ".md", ".jpg", ".jpeg", ".pkl", ".yml", ".yaml", ".toml")
REQS_PATHS = {"django/django": ["tests/requirements/py3.txt"],
              "pylint-dev/pylint": ["requirements_test.txt"]}

_SPHINX_3X = [
    "sed -i 's/Jinja2>=2.3/Jinja2<3.0/' setup.py",
    "sed -i 's/sphinxcontrib-applehelp/sphinxcontrib-applehelp<=1.0.7/' setup.py",
    "sed -i 's/sphinxcontrib-devhelp/sphinxcontrib-devhelp<=1.0.5/' setup.py",
    "sed -i 's/sphinxcontrib-qthelp/sphinxcontrib-qthelp<=1.0.6/' setup.py",
    "sed -i 's/alabaster>=0.7,<0.8/alabaster>=0.7,<0.7.12/' setup.py",
    "sed -i \"s/'packaging',/'packaging', 'markupsafe<=2.0.1',/\" setup.py",
    "sed -i 's/sphinxcontrib-htmlhelp/sphinxcontrib-htmlhelp<=2.0.4/' setup.py",
    "sed -i 's/sphinxcontrib-serializinghtml/sphinxcontrib-serializinghtml<=1.1.9/' setup.py",
]
_PYTEST_PINS = {
    "5.2": ["atomicwrites==1.4.1", "attrs==23.1.0", "more-itertools==10.1.0", "packaging==23.1",
            "pluggy==0.13.1", "py==1.11.0", "wcwidth==0.2.6"],
    "5.4": ["py==1.11.0", "packaging==23.1", "attrs==23.1.0", "more-itertools==10.1.0", "pluggy==0.13.1"],
    "6.0": ["attrs==23.1.0", "iniconfig==2.0.0", "more-itertools==10.1.0", "packaging==23.1",
            "pluggy==0.13.1", "py==1.11.0", "toml==0.10.2"],
}


def spec(repo: str, version: str) -> dict:
    """The SWE-bench v4.1.0 spec for the repo/versions this probe uses."""
    if repo == "django/django":
        py = {"3.0": "3.6", "3.1": "3.6", "4.1": "3.9", "4.2": "3.9", "5.0": "3.11"}.get(version, "3.9")
        s = {"python": py, "reqs": True, "install": "python -m pip install -e .", "pip": [],
             "pre": [], "test_cmd": TEST_DJANGO}
        if version in ("3.0", "3.1"):
            s["eval_env"] = {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "LANGUAGE": "en_US:en"}
        return s
    if repo == "sympy/sympy":
        return {"python": "3.9", "reqs": False, "install": "python -m pip install -e .",
                "pip": ["mpmath==1.3.0", "flake8", "flake8-comprehensions"], "pre": [], "test_cmd": TEST_SYMPY}
    if repo == "pytest-dev/pytest":
        return {"python": "3.9", "reqs": False, "install": "python -m pip install -e .",
                "pip": _PYTEST_PINS.get(version, []), "pre": [], "test_cmd": TEST_PYTEST}
    if repo == "sphinx-doc/sphinx":
        pre = ["sed -i 's/pytest/pytest -rA/' tox.ini"]
        if version[:1] in ("3", "4") and version not in ("4.5",):
            pre += _SPHINX_3X
        return {"python": "3.9", "reqs": False, "install": "python -m pip install -e .[test]",
                "pip": ["tox==4.16.0", "tox-current-env==0.0.11", "Jinja2==3.0.3"], "pre": pre,
                "test_cmd": TEST_SPHINX}
    if repo == "pylint-dev/pylint":
        return {"python": "3.9", "reqs": True, "install": "python -m pip install -e .", "pip": [],
                "pre": [], "test_cmd": TEST_PYTEST}
    raise ValueError(f"no spec for {repo}")


# --------------------------------------------------------------------------
# dataset
# --------------------------------------------------------------------------


def load_rows(ids: list[str], data_dir: str, dataset_file: str = "", fetch: bool = True) -> dict:
    """{instance_id: row} for `ids`. Order of sources: data_dir/rows.json (the
    cache), `dataset_file` (a datasets-server rows dump or a JSON list of
    rows, e.g. a copy you already have), then the HF datasets-server API.
    Whatever is found is cached in data_dir/rows.json."""
    cache = os.path.join(data_dir, "rows.json")
    rows = {}
    if os.path.isfile(cache):
        with open(cache, encoding="utf-8") as f:
            rows = json.load(f)
    want = set(ids)
    if want - set(rows) and dataset_file:
        for path in _expand(dataset_file):
            rows.update({r["instance_id"]: r for r in _rows_of(path) if r["instance_id"] in want})
    if want - set(rows) and fetch:
        offset = 0
        while want - set(rows):
            with urllib.request.urlopen(ROWS_URL.format(offset=offset), timeout=60) as r:
                page = json.loads(r.read().decode("utf-8"))
            got = [x["row"] for x in page.get("rows", [])]
            rows.update({x["instance_id"]: x for x in got if x["instance_id"] in want})
            offset += 100
            if not got or offset >= page.get("num_rows_total", 0):
                break
    missing = sorted(want - set(rows))
    if missing:
        raise SystemExit(f"dataset rows missing for: {', '.join(missing)}")
    os.makedirs(data_dir, exist_ok=True)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    os.replace(tmp, cache)
    return {i: rows[i] for i in ids}


def _expand(path: str) -> list[str]:
    if os.path.isdir(path):
        return sorted(os.path.join(path, n) for n in os.listdir(path) if n.endswith(".json"))
    return [path]


def _rows_of(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, dict) and "rows" in d:
        return [x["row"] if "row" in x else x for x in d["rows"]]
    if isinstance(d, dict):
        return list(d.values())
    return d


def f2p_p2p(row: dict) -> tuple[list[str], list[str]]:
    def lst(v):
        return json.loads(v) if isinstance(v, str) else list(v or [])
    return lst(row["FAIL_TO_PASS"]), lst(row["PASS_TO_PASS"])


# --------------------------------------------------------------------------
# eval semantics
# --------------------------------------------------------------------------


def test_files(test_patch: str) -> list[str]:
    return re.findall(r"diff --git a/.* b/(.*)", test_patch)


def test_directives(row: dict) -> list[str]:
    d = [p for p in test_files(row["test_patch"]) if not any(p.endswith(e) for e in NON_TEST_EXTS)]
    if row["repo"] == "django/django":
        out = []
        for p in d:
            p = p[: -len(".py")] if p.endswith(".py") else p
            p = p[len("tests/"):] if p.startswith("tests/") else p
            out.append(p.replace("/", "."))
        return out
    return d


_STATUSES = ("FAILED", "PASSED", "SKIPPED", "ERROR", "XFAIL")


def parse_pytest(log: str) -> dict:
    m = {}
    for line in log.split("\n"):
        if line.startswith(_STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            tc = line.split()
            if len(tc) > 1:
                m[tc[1]] = tc[0]
    return m


def parse_pytest_options(log: str) -> dict:
    opt = re.compile(r"(.*?)\[(.*)\]")
    m = {}
    for line in log.split("\n"):
        if line.startswith(_STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            tc = line.split()
            if len(tc) <= 1:
                continue
            h = opt.search(tc[1])
            if h:
                main, o = h.groups()
                if o.startswith("/") and not o.startswith("//") and "*" not in o:
                    o = "/" + o.split("/")[-1]
                m[f"{main}[{o}]"] = tc[0]
            else:
                m[tc[1]] = tc[0]
    return m


def parse_pytest_v2(log: str) -> dict:
    esc = str.maketrans("", "", "".join(chr(c) for c in range(1, 32)))
    m = {}
    for line in log.split("\n"):
        line = re.sub(r"\[(\d+)m", "", line).translate(esc)
        if line.startswith(_STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            tc = line.split()
            if len(tc) >= 2:
                m[tc[1]] = tc[0]
        elif line.endswith(_STATUSES):
            tc = line.split()
            if len(tc) >= 2:
                m[tc[0]] = tc[1]
    return m


def parse_django(log: str) -> dict:
    m = {}
    prev = None
    for line in log.split("\n"):
        line = line.strip()
        if "--version is equivalent to version" in line:
            m["--version is equivalent to version"] = "PASSED"
        if " ... " in line:
            prev = line.split(" ... ")[0]
        for suf in (" ... ok", " ... OK", " ...  OK"):
            if line.endswith(suf):
                if line.startswith("Applying sites.0002_alter_domain_unique...test_no_migrations"):
                    line = line.split("...", 1)[-1].strip()
                m[line.rsplit(suf, 1)[0]] = "PASSED"
                break
        if " ... skipped" in line:
            m[line.split(" ... skipped")[0]] = "SKIPPED"
        if line.endswith(" ... FAIL"):
            m[line.split(" ... FAIL")[0]] = "FAILED"
        if line.startswith("FAIL:"):
            m[line.split()[1].strip()] = "FAILED"
        if line.endswith(" ... ERROR"):
            m[line.split(" ... ERROR")[0]] = "ERROR"
        if line.startswith("ERROR:"):
            m[line.split()[1].strip()] = "ERROR"
        if line.lstrip().startswith("ok") and prev is not None:
            m[prev] = "PASSED"
    for pat in (r"^(.*?)\s\.\.\.\sTesting\ against\ Django\ installed\ in\ ((?s:.*?))\ silenced\)\.\nok$",
                r"^(.*?)\s\.\.\.\sInternal\ Server\ Error:\ \/(.*)\/\nok$",
                r"^(.*?)\s\.\.\.\sSystem check identified no issues \(0 silenced\)\nok$"):
        for x in re.finditer(pat, log, re.MULTILINE):
            m[x.group(1)] = "PASSED"
    return m


def parse_sympy(log: str) -> dict:
    m = {}
    for x in re.findall(r"(_*) (.*)\.py:(.*) (_*)", log):
        m[f"{x[1]}.py:{x[2]}"] = "FAILED"
    for line in log.split("\n"):
        line = line.strip()
        if line.startswith("test_"):
            if line.endswith(" E"):
                m[line.split()[0]] = "ERROR"
            if line.endswith(" F"):
                m[line.split()[0]] = "FAILED"
            if line.endswith(" ok"):
                m[line.split()[0]] = "PASSED"
    return m


PARSERS = {"django/django": parse_django, "sympy/sympy": parse_sympy, "pytest-dev/pytest": parse_pytest,
           "sphinx-doc/sphinx": parse_pytest_v2, "pylint-dev/pylint": parse_pytest_options}


def grade(status: dict, f2p: list[str], p2p: list[str]) -> dict:
    """SWE-bench grading: a test passes iff its status is PASSED or XFAIL; a
    missing test counts as failed. Resolved = every F2P and every P2P passes."""
    ok = lambda t: status.get(t) in ("PASSED", "XFAIL")  # noqa: E731
    f_fail = [t for t in f2p if not ok(t)]
    p_fail = [t for t in p2p if not ok(t)]
    return {"resolved": not f_fail and not p_fail, "f2p_pass": len(f2p) - len(f_fail), "f2p": len(f2p),
            "p2p_pass": len(p2p) - len(p_fail), "p2p": len(p2p), "f2p_failed": f_fail[:10], "p2p_failed": p_fail[:10]}


# --------------------------------------------------------------------------
# env build
# --------------------------------------------------------------------------


def _kill_tree(proc: subprocess.Popen) -> None:
    """The whole tree, not just bash: an orphaned test runner keeps running,
    and on Windows keeps the pipes open so collecting output never returns."""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


def _run(cmd, cwd=None, timeout=1800, env=None, check=True, nice=True, raw=False) -> subprocess.CompletedProcess:
    """`raw` keeps stdout as bytes (a diff whose CRs must survive); otherwise
    UTF-8 text whatever the locale (Windows defaults to cp1252). On a timeout
    or interrupt the child's whole process tree is killed."""
    if isinstance(cmd, str):
        cmd = [BASH, "-c", cmd]
    text = {} if raw else {"encoding": "utf-8", "errors": "replace"}
    if IS_WINDOWS:
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
              | (subprocess.BELOW_NORMAL_PRIORITY_CLASS if nice else 0)}
    else:
        kw = {"start_new_session": True}
    with subprocess.Popen((NICE if nice else []) + cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **text, **kw) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException as e:
            _kill_tree(proc)
            out, err = proc.communicate()
            if isinstance(e, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err) from None
            raise
    r = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if check and r.returncode != 0:
        err = r.stderr or r.stdout
        err = err.decode("utf-8", "replace") if raw else err
        raise RuntimeError(f"{' '.join(cmd)[:200]} -> exit {r.returncode}: {err[-800:]}")
    return r


def _rmtree(path: str) -> None:
    """rmtree that also removes read-only files (git's objects on Windows)."""
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if os.path.isdir(path):
        shutil.rmtree(path, **({"onexc": onerror} if sys.version_info >= (3, 12) else {"onerror": onerror}))


def venv_bin(venv: str) -> str:
    return os.path.join(venv, "Scripts" if IS_WINDOWS else "bin")


def venv_python(venv: str) -> str:
    return os.path.join(venv_bin(venv), "python.exe" if IS_WINDOWS else "python")


def _py_launcher(v: str) -> str:
    """Windows installs register with `py -3.X`, not as python3.X on PATH."""
    if not (IS_WINDOWS and shutil.which("py")):
        return ""
    r = subprocess.run(["py", f"-{v}", "-c", "import sys; print(sys.executable)"], capture_output=True,
                       text=True, stdin=subprocess.DEVNULL, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def find_python(want: str) -> tuple[str, str]:
    """(interpreter path, note). Uses an installed interpreter only (never
    downloads one); falls back to the nearest newer installed 3.x and says so."""
    cands = []
    for v in [want] + [f"3.{n}" for n in range(int(want.split(".")[1]) + 1, 15)]:
        p = shutil.which(f"python{v}") or _py_launcher(v)
        if not p and shutil.which("uv"):
            r = subprocess.run(["uv", "python", "find", "--no-python-downloads", v],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
            p = r.stdout.strip() if r.returncode == 0 else ""
        if not p and v == "%d.%d" % sys.version_info[:2]:
            p = sys.executable
        if p:
            cands.append((v, p))
            break
    if not cands:
        raise RuntimeError(f"no python >= {want} installed")
    v, p = cands[0]
    return p, ("" if v == want else f"python {want} not installed; used {v}")


def _clone(repo: str, base: str, repos_dir: str) -> str:
    dst = os.path.join(repos_dir, repo.replace("/", "__"))
    if not os.path.isdir(os.path.join(dst, ".git")) and not os.path.isfile(os.path.join(dst, "HEAD")):
        os.makedirs(repos_dir, exist_ok=True)
        _run(["git", "clone", "--bare", "--filter=blob:none", f"https://github.com/{repo}.git", dst],
             timeout=3600)
    if _run(["git", "-C", dst, "cat-file", "-e", base + "^{commit}"], check=False).returncode != 0:
        _run(["git", "-C", dst, "fetch", "origin", base], timeout=1800)
    return dst


def requirements(bare: str, repo: str, commit: str) -> str:
    excl = lambda ln: ln.strip().startswith(("-e .", "#", ".[test"))  # noqa: E731
    for path in REQS_PATHS.get(repo, []):
        r = _run(["git", "-C", bare, "show", f"{commit}:{path}"], check=False, nice=False)
        if r.returncode != 0:
            continue
        orig, extra = [], []
        d = "/".join(path.split("/")[:-1])
        for ln in r.stdout.split("\n"):
            if ln.strip().startswith("-r"):
                inc = _run(["git", "-C", bare, "show", f"{commit}:{(d + '/' if d else '') + ln[2:].strip()}"],
                           check=False, nice=False)
                if inc.returncode == 0:
                    extra += [x for x in inc.stdout.split("\n") if not excl(x)]
            elif not excl(ln):
                orig.append(ln)
        extra.append("\n".join(orig))
        return "\n".join(extra)
    return ""


def env_paths(cache: str, iid: str) -> dict:
    """envs/<id>/ is what the agent can see next to its checkout; anything
    derived from the gold or test patch (gate result and logs) lives under
    data/ with the dataset rows instead."""
    root = os.path.join(cache, "envs", iid)
    gates = os.path.join(cache, "data", "gates", iid)
    return {"root": root, "src": os.path.join(root, "src"), "venv": os.path.join(root, "venv"),
            "meta": os.path.join(root, "env.json"), "gates": gates, "gate": os.path.join(gates, "gate.json")}


_GIT_ID = ["-c", "user.name=bench", "-c", "user.email=bench@localhost", "-c", "commit.gpgsign=false"]
# LF in every checkout whatever the user's git config (Git for Windows ships
# core.autocrlf=true): the gold and test patches are LF and would not apply.
_GIT_LF = (("core.autocrlf", "false"), ("core.eol", "lf"))


def make_checkout(bare: str, commit: str, src: str) -> None:
    """`commit`'s tree in `src` as its own git repo (no history, no remote):
    opencode takes it as the project root instead of walking up to whatever
    repo contains the cache, and the agent gets a working `git diff`.
    Extracted with tarfile, not `| tar -x`: no shell quoting, and no MSYS tar
    handed a Windows drive path."""
    os.makedirs(src, exist_ok=True)
    lf = [a for k, v in _GIT_LF for a in ("-c", f"{k}={v}")]
    tar = os.path.join(os.path.dirname(src), "src.tar")
    try:
        _run(["git"] + lf + ["-C", bare, "archive", "--format=tar", "-o", tar, commit], timeout=1800)
        with tarfile.open(tar) as tf:
            tf.extractall(src, **({"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}))
    finally:
        if os.path.exists(tar):
            os.remove(tar)
    _run(["git", "-C", src, "init", "-q"], nice=False)
    for k, v in _GIT_LF:
        _run(["git", "-C", src, "config", k, v], nice=False)


def commit_baseline(src: str) -> None:
    """One local commit + tag `pristine`: every trial and eval resets to it."""
    g = ["git", "-C", src] + _GIT_ID
    _run(g + ["add", "-A"], nice=False)
    _run(g + ["commit", "-q", "-m", "pristine", "--allow-empty"], nice=False)
    _run(g + ["tag", "-f", "pristine"], nice=False)


def build_env(row: dict, cache: str, rebuild: bool = False, logf=print) -> dict:
    """Checkout of base_commit with no git history (so the fix cannot be read
    from a later commit), a venv beside it, the spec's install steps, then one
    commit "pristine" that every trial and eval resets to."""
    p = env_paths(cache, row["instance_id"])
    if not rebuild and os.path.isfile(p["meta"]):
        with open(p["meta"], encoding="utf-8") as f:
            return json.load(f)
    _rmtree(p["root"])
    os.makedirs(p["src"])
    s = spec(row["repo"], row["version"])
    notes = []
    logf(f"  {row['instance_id']}: clone/archive {row['repo']}@{row['base_commit'][:10]}")
    bare = _clone(row["repo"], row["base_commit"], os.path.join(cache, "repos"))
    make_checkout(bare, row["base_commit"], p["src"])
    for c in s["pre"]:
        if "apt-get" in c or "locale-gen" in c:
            notes.append(f"skipped (needs root): {c}")
            continue
        _run(c, cwd=p["src"], check=False)
    py, note = find_python(s["python"])
    if note:
        notes.append(note)
    _run([py, "-m", "venv", p["venv"]])
    vpy = venv_python(p["venv"])
    pip = [vpy, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    if s["reqs"]:
        reqs = requirements(bare, row["repo"], row["environment_setup_commit"])
        if reqs.strip():
            rf = os.path.join(p["root"], "requirements.txt")
            with open(rf, "w", encoding="utf-8") as f:
                f.write(reqs)
            if _run(pip + ["-r", rf], timeout=3600, check=False).returncode != 0:
                bad = []
                for ln in reqs.splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith("#") and _run(pip + [ln], check=False).returncode != 0:
                        bad.append(ln)
                if bad:
                    notes.append("requirements that did not install: " + ", ".join(bad))
    if s["pip"]:
        _run(pip + s["pip"], timeout=3600)
    venv_env = dict(os.environ, VIRTUAL_ENV=p["venv"],
                    PATH=venv_bin(p["venv"]) + os.pathsep + os.environ.get("PATH", ""),
                    # No git history here, so setuptools_scm (pytest) would say 0.1.dev0 and
                    # the repo's own `minversion` checks fail; pin it to the task's version.
                    SETUPTOOLS_SCM_PRETEND_VERSION=row["version"] + ".0")
    _run(s["install"], cwd=p["src"], env=venv_env, timeout=3600)
    if row["repo"] == "pytest-dev/pytest" or "pytest" in s["test_cmd"]:
        _run([vpy, "-c", "import pytest"], env=venv_env)
    commit_baseline(p["src"])
    meta = {"id": row["instance_id"], "src": p["src"], "venv": p["venv"], "python": py,
            "notes": notes, "built": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(p["meta"], "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    return meta


def reset(env: dict) -> None:
    _run(["git", "-C", env["src"], "reset", "-q", "--hard", "pristine"], nice=False)
    _run(["git", "-C", env["src"], "clean", "-qfd"], nice=False)


def capture_diff(env: dict) -> str:
    """Everything the agent changed, new files included (SWE-bench model_patch)."""
    g = ["git", "-C", env["src"]]
    _run(g + ["add", "-A"], nice=False)
    r = _run(g + ["diff", "--cached", "--binary", "pristine"], nice=False, raw=True)
    _run(g + ["reset", "-q"], nice=False)
    return r.stdout.decode("utf-8", "surrogateescape")


def evaluate(env: dict, row: dict, patch: str, timeout: int = 1800, log_path: str = "") -> dict:
    """SWE-bench eval on a fresh reset: apply `patch` (may be empty), reset the
    test files to base, apply the gold test_patch, run the test command on the
    test-patch files, parse, grade."""
    reset(env)
    s = spec(row["repo"], row["version"])
    out = {"applied": True}
    tmp = tempfile.mkdtemp(prefix="hb-thoughts-eval-")
    try:
        if patch.strip():
            pf = os.path.join(tmp, "model.patch")
            with open(pf, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                f.write(patch)
            r = _run(["git", "-C", env["src"], "apply", "--whitespace=nowarn", pf], check=False, nice=False)
            if r.returncode != 0 and shutil.which("patch"):
                r = _run(["patch", "-p1", "--batch", "--fuzz=5", "-d", env["src"], "-i", pf], check=False, nice=False)
            if r.returncode != 0:
                reset(env)
                return {"applied": False, "resolved": False, "error": "patch did not apply"}
        tf = os.path.join(tmp, "test.patch")
        with open(tf, "w", encoding="utf-8", newline="") as f:
            f.write(row["test_patch"])
        files = test_files(row["test_patch"])
        existing = [x for x in files if _run(["git", "-C", env["src"], "cat-file", "-e", f"pristine:{x}"],
                                             check=False, nice=False).returncode == 0]
        if existing:
            _run(["git", "-C", env["src"], "checkout", "pristine", "--"] + existing, nice=False)
        _run(["git", "-C", env["src"], "apply", "--whitespace=nowarn", tf], nice=False)
        envv = dict(os.environ, VIRTUAL_ENV=env["venv"],
                    PATH=venv_bin(env["venv"]) + os.pathsep + os.environ.get("PATH", ""))
        for k in [k for k in envv if k.startswith(("PYTEST_", "PYTHON"))]:
            envv.pop(k)
        if IS_WINDOWS:
            # Piped stdout is cp1252 there; a non-cp1252 test name would kill the runner.
            envv["PYTHONUTF8"] = "1"
        envv.update(s.get("eval_env", {}))
        cmd = s["test_cmd"] + " " + " ".join(test_directives(row))
        t0 = time.monotonic()
        try:
            r = _run(cmd, cwd=env["src"], env=envv, timeout=timeout, check=False)
            text, out["rc"] = r.stdout + "\n" + r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            so = e.stdout or ""
            text = so.decode("utf-8", "replace") if isinstance(so, bytes) else so
            out["rc"], out["timed_out"] = -9, True
        out["secs"] = round(time.monotonic() - t0, 1)
        if log_path:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"$ {cmd}\n{text}")
        f2p, p2p = f2p_p2p(row)
        out.update(grade(PARSERS[row["repo"]](text), f2p, p2p))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        reset(env)
    return out


GATE_NO_FAIL = "every FAIL_TO_PASS test already passes without the fix"
GATE_UNRESOLVED = "the gold patch does not resolve the task in this env"


def gold_gate(row: dict, cache: str, rebuild: bool = False, logf=print) -> dict:
    """Build the env, then require (1) with only the test patch, at least one
    F2P test fails, and (2) with the gold patch, the task is resolved. Result
    cached in data/gates/<id>/gate.json; ok=False excludes the task. Only a
    verdict is reused: an env that failed to build is retried next run."""
    p = env_paths(cache, row["instance_id"])
    if not rebuild and os.path.isfile(p["gate"]):
        with open(p["gate"], encoding="utf-8") as f:
            res = json.load(f)
        if res.get("ok") or res.get("why") in (GATE_NO_FAIL, GATE_UNRESOLVED):
            return res
    t0 = time.monotonic()
    os.makedirs(p["gates"], exist_ok=True)
    res = {"id": row["instance_id"], "ok": False}
    try:
        env = build_env(row, cache, rebuild, logf)
        res["notes"] = env["notes"]
        logf(f"  {row['instance_id']}: baseline eval (test patch only)")
        base = evaluate(env, row, "", log_path=os.path.join(p["gates"], "gate-baseline.log"))
        logf(f"  {row['instance_id']}: gold eval")
        gold = evaluate(env, row, row["patch"], log_path=os.path.join(p["gates"], "gate-gold.log"))
        res["baseline"] = {k: base.get(k) for k in ("f2p_pass", "f2p", "p2p_pass", "p2p")}
        res["gold"] = {k: gold.get(k) for k in ("resolved", "f2p_pass", "f2p", "p2p_pass", "p2p",
                                                  "f2p_failed", "p2p_failed")}
        if base.get("f2p_pass", 0) == base.get("f2p", 0):
            res["why"] = GATE_NO_FAIL
        elif not gold.get("resolved"):
            res["why"] = GATE_UNRESOLVED
        else:
            res["ok"] = True
    except Exception as e:  # noqa: BLE001 - recorded, the task is excluded
        res["why"] = f"{type(e).__name__}: {str(e)[-600:]}"
    res["secs"] = round(time.monotonic() - t0, 1)
    os.makedirs(p["gates"], exist_ok=True)
    with open(p["gate"], "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    return res


if __name__ == "__main__":
    sys.exit("use run.py --gate-only")
