#!/usr/bin/env python3
"""
Host build MCP — template.

This server runs ON THE HOST (e.g. a Windows machine with Visual Studio /
MSVC / CMake), NOT inside a harness agent container. The harness agent runs
in a Linux container and cannot build a native Windows project; it reaches
this server over HTTP at http://host.docker.internal:<port>/mcp and calls the
tools below to configure, build, test, and run the project on the host. The
project source is bind-mounted into the agent container at the same path, so
the agent edits files and this server builds those same files on the host.

This is a TEMPLATE. `harness mcp host-init <name>` copies it to
`host-mcp/<name>/` and fills in the name/port. A setup agent (launched by
`harness mcp host-setup <name>`, which reads AGENTS.md in that folder) then
tailors `project.json` and prunes the tool set to what the project needs.

Customize: edit project.json (paths, generator, targets), keep only the
tools the project needs, and add project-specific tools if useful. Verify the
MCP SDK calls against the installed version — `pip show mcp`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# --- config -----------------------------------------------------------------
# project.json lives next to this file. host-init writes it; the setup agent
# fills in the real values. Env vars override so the launch script can pin
# host/port without editing the file.

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "project.json"


def _load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[host-mcp] project.json is not valid JSON: {exc}")
    cfg.setdefault("name", "__MCP_NAME__")
    # Bind on 0.0.0.0 so the agent container can reach it; localhost would
    # only accept connections from the host itself.
    cfg.setdefault("host", os.environ.get("HOST_MCP_HOST", "0.0.0.0"))
    cfg.setdefault("port", int(os.environ.get("HOST_MCP_PORT", "__MCP_PORT__")))
    cfg["port"] = int(os.environ.get("HOST_MCP_PORT", cfg["port"]))
    # Absolute path to the project root on the HOST. The agent sees this same
    # path inside its container (harness bind-mounts CWD at the same path).
    cfg.setdefault("project_dir", str(HERE))
    # Where CMake writes its build tree (relative to project_dir or absolute).
    cfg.setdefault("build_dir", "build")
    # CMake generator, e.g. "Visual Studio 17 2022". Empty = CMake default.
    cfg.setdefault("generator", "")
    # Default build configuration for multi-config generators.
    cfg.setdefault("config", "Debug")
    # Allowlist of build/run targets the agent may name. Empty list = allow
    # any target (looser; tighten for untrusted callers).
    cfg.setdefault("targets", [])
    # Cap on captured output bytes returned to the agent, so a huge build log
    # can't blow the agent's context.
    cfg.setdefault("max_output_bytes", 60_000)
    # Per-command wall-clock timeout (seconds).
    cfg.setdefault("timeout_seconds", 1800)
    return cfg


CFG = _load_config()

mcp = FastMCP(CFG["name"], host=CFG["host"], port=CFG["port"])

# Last build's captured output, so get_build_errors can re-scan it without
# rebuilding. Reset on each build/configure.
_LAST_BUILD_OUTPUT = ""


# --- helpers ----------------------------------------------------------------

def _project_dir() -> Path:
    return Path(CFG["project_dir"]).resolve()


def _build_dir() -> Path:
    bd = Path(CFG["build_dir"])
    return bd if bd.is_absolute() else (_project_dir() / bd)


def _check_target(target: str) -> None:
    allow = CFG.get("targets") or []
    if allow and target not in allow:
        raise ValueError(
            f"target '{target}' is not in the allowed targets {allow}; "
            "add it to project.json 'targets' to permit it."
        )


def _run(cmd: list[str], cwd: Path) -> str:
    """Run a command on the host, capturing stdout+stderr and the exit code.

    Returns a single text block the agent reads: the command, exit code, and
    truncated combined output. This is request/response: the whole command
    runs to completion, then its output is returned (MCP does not stream
    tokens mid-call).
    """
    printable = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=CFG["timeout_seconds"],
        )
    except FileNotFoundError:
        return f"$ {printable}\n[error] command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return f"$ {printable}\n[error] timed out after {CFG['timeout_seconds']}s"

    out = (proc.stdout or "") + (proc.stderr or "")
    cap = CFG["max_output_bytes"]
    if len(out) > cap:
        out = out[:cap] + f"\n[...truncated {len(out) - cap} bytes...]"
    return f"$ {printable}\nexit code: {proc.returncode}\n\n{out}".rstrip()


# --- tools ------------------------------------------------------------------
# Keep the ones the project needs; delete the rest. Each tool's docstring is
# what the agent reads to decide when to call it, so keep them accurate.

# Orientation tool. KEEP THIS — it is what the agent calls FIRST to learn the
# build's state before touching configure/build/test, so it stops blindly
# calling tools against an un-scaffolded tree and reading back errors. It is
# read-only and cheap: it inspects the filesystem, it does not run cmake.
# Adapt what it reports to your build system (the MSBuild path checks for a
# different marker than CMakeCache.txt), but keep one such tool.
@mcp.tool()
def project_state() -> str:
    """Report what this MCP manages and the project's current build state.

    CALL THIS FIRST, before configure/build/test. It tells you whether the
    project is already configured/built or still needs scaffolding, so you act
    on the real state instead of calling build() blind and reading back errors.
    Read-only and cheap — it inspects the filesystem, it does not run cmake.
    """
    pdir = _project_dir()
    bdir = _build_dir()
    configured = (bdir / "CMakeCache.txt").exists()
    allow = CFG.get("targets") or []
    lines = [
        f"MCP {CFG['name']}: builds a native project on this host via CMake; "
        "the agent edits the source, this server compiles it here.",
        f"project_dir: {pdir} ({'present' if pdir.exists() else 'MISSING'})",
        f"build_dir:   {bdir} ({'present' if bdir.exists() else 'absent'})",
        f"configured:  {'yes (CMakeCache.txt present)' if configured else 'NO'}",
        f"generator:   {CFG.get('generator') or '(cmake default)'}",
        f"config:      {CFG.get('config') or '(generator default)'}",
        f"targets:     {', '.join(allow) if allow else '(no allowlist — any target)'}",
    ]
    if _LAST_BUILD_OUTPUT:
        tail = next(
            (ln for ln in _LAST_BUILD_OUTPUT.splitlines() if "exit code:" in ln),
            "ran this session",
        )
        lines.append(f"last build:  {tail.strip()}")
    else:
        lines.append("last build:  none this session")
    lines.append(
        "next: call configure(), then build()." if not configured
        else "next: configure() already ran — call build() (or configure() "
        "again after editing CMakeLists)."
    )
    return "\n".join(lines)


@mcp.tool()
def configure() -> str:
    """Run CMake configure/generate for the project (cmake -S . -B <build_dir>).

    Call this after editing CMakeLists.txt or on a fresh checkout, before build.
    """
    global _LAST_BUILD_OUTPUT
    cmd = ["cmake", "-S", str(_project_dir()), "-B", str(_build_dir())]
    if CFG.get("generator"):
        cmd += ["-G", CFG["generator"]]
    result = _run(cmd, _project_dir())
    _LAST_BUILD_OUTPUT = result
    return result


@mcp.tool()
def build(target: str = "", config: str = "") -> str:
    """Build the project (cmake --build <build_dir>).

    target: optional specific target; empty builds the default/all.
    config: build configuration (e.g. Debug, Release); empty uses project default.
    Returns the compiler output and exit code. Read it for errors, then fix and
    rebuild.
    """
    global _LAST_BUILD_OUTPUT
    cmd = ["cmake", "--build", str(_build_dir())]
    cfg = config or CFG.get("config")
    if cfg:
        cmd += ["--config", cfg]
    if target:
        _check_target(target)
        cmd += ["--target", target]
    result = _run(cmd, _project_dir())
    _LAST_BUILD_OUTPUT = result
    return result


@mcp.tool()
def clean() -> str:
    """Clean build artifacts (cmake --build <build_dir> --target clean)."""
    cmd = ["cmake", "--build", str(_build_dir()), "--target", "clean"]
    return _run(cmd, _project_dir())


@mcp.tool()
def run_tests(ctest_args: str = "") -> str:
    """Run the test suite via CTest in the build dir.

    ctest_args: extra CTest args (e.g. "-R MyTest" to filter). Space-separated.
    """
    cmd = ["ctest", "--output-on-failure"]
    cfg = CFG.get("config")
    if cfg:
        cmd += ["-C", cfg]
    if ctest_args:
        cmd += shlex.split(ctest_args)
    return _run(cmd, _build_dir())


@mcp.tool()
def list_targets() -> str:
    """List the CMake build targets available in the configured build tree."""
    cmd = ["cmake", "--build", str(_build_dir()), "--target", "help"]
    return _run(cmd, _project_dir())


@mcp.tool()
def get_build_errors() -> str:
    """Return just the error/warning lines from the most recent build/configure.

    Cheaper to read than the full log when you only need the diagnostics.
    """
    if not _LAST_BUILD_OUTPUT:
        return "no build has run yet this session; call build() first."
    lines = [
        ln for ln in _LAST_BUILD_OUTPUT.splitlines()
        if any(k in ln.lower() for k in ("error", "warning", "failed", "fatal"))
    ]
    if not lines:
        return "no error/warning lines found in the last build output."
    cap = CFG["max_output_bytes"]
    text = "\n".join(lines)
    return text[:cap]


# Optional: run a built executable and capture its output. Delete if the
# project never needs the agent to run binaries. Tighten via 'targets'.
@mcp.tool()
def run_target(target: str, args: str = "") -> str:
    """Run a built executable target and capture its output.

    target: the executable target name (must be in project.json 'targets' if
    that allowlist is non-empty). args: space-separated command-line args.
    """
    _check_target(target)
    exe = _build_dir() / target
    cmd = [str(exe)] + (shlex.split(args) if args else [])
    return _run(cmd, _build_dir())


if __name__ == "__main__":
    # Streamable-HTTP is the current MCP transport; the endpoint is
    # http://<host>:<port>/mcp. opencode's "remote" MCP type connects to it.
    print(
        f"[host-mcp] {CFG['name']} serving on "
        f"http://{CFG['host']}:{CFG['port']}/mcp (project: {_project_dir()})",
        flush=True,
    )
    mcp.run(transport="streamable-http")
