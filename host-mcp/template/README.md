# Host build MCP (template)

An MCP server that runs **on the host** and lets a containerized harness agent
build, test, and run a native project that lives on the host. Built for the
case where the agent runs in Linux but the project needs Windows + Visual
Studio / MSVC / CMake to compile.

This folder is a **template**. You do not edit it directly. Instead:

```
harness mcp host-init <name>      # copy this template to host-mcp/<name>/, pick a port,
                                  #   and register it as a host MCP for the agent
harness mcp host-setup <name>     # launch a setup agent inside host-mcp/<name>/ that reads
                                  #   AGENTS.md and tailors server.py + project.json with you
```

Then, on the host, in `host-mcp/<name>/`:

```
./run.sh      # Linux, macOS, and Windows Git Bash (the same Git Bash you run harness in)
```

That is all. Reachability is automatic: while the host MCP is enabled, harness
maps `host.docker.internal` into the agent container and opens it through the
firewall. No `harness net allow` and no `--add-host` are needed.

## How it fits together

```
agent container  --HTTP-->  host.docker.internal:<port>/mcp  -->  this server  -->  cmake/msbuild on the host
   (edits source)                                                                   (compiles that source)
```

- The agent and the host see the **same project files** (harness bind-mounts
  the working directory into the container at the same path).
- The agent edits files and calls the build tools; this server runs the real
  toolchain on the host and returns the output.
- It is request/response: each tool call runs a command to completion and
  returns its captured stdout/stderr + exit code (truncated).

## Files

- `server.py` — the MCP server. Tools: `configure`, `build`, `clean`,
  `run_tests`, `list_targets`, `get_build_errors`, `run_target`. Prune to what
  the project needs.
- `project.json` — project paths, CMake generator, config, target allowlist,
  output/timeout caps. Filled in during host-setup.
- `requirements.txt` — the `mcp` Python SDK.
- `run.sh` — venv + install + launch. One git-bash-native script for Linux,
  macOS, and Windows Git Bash (no PowerShell).
- `AGENTS.md` — instructions the host-setup agent reads.

## Notes

- This is **not a Docker MCP.** It has no `compose.yml` and is not managed by
  `harness mcp up/down`. It runs as a plain process on the host; you start and
  stop it with the run script.
- The port is chosen at `host-init` time and written into both the agent's MCP
  config and `project.json`. Changing it means re-running `host-init` (or
  setting `HOST_MCP_PORT` and editing the agent config to match).
- Security: the server runs whatever build commands its tools construct. Keep
  the `targets` allowlist populated if you expose `run_target`, and do not add
  tools that execute arbitrary agent-supplied shell strings.
