# Host build MCP — setup agent instructions

You are an agent launched inside a **host build MCP** project folder by
`harness mcp host-setup <name>`. Your job: turn this copied template into a
working MCP that lets a containerized harness agent build, test, and run a
native project that lives on the **host** (typically a Windows machine with
Visual Studio / MSVC / CMake).

Read this whole file before doing anything. The files in this folder
(`server.py`, `project.json`, `run.sh`, `requirements.txt`) are a
scaffold — your job is to tailor them to one specific project and hand the
user a server they can launch.

## Why this exists

The harness agent runs in a Linux container. It can edit a Windows/MSVC
project's source (the project is bind-mounted into the container) but it
cannot compile it — there is no Visual Studio in the container. This MCP runs
on the host, where the toolchain is, and exposes build/test/run as MCP tools
over HTTP. The agent calls those tools; the host does the compiling. That is
the whole point: **the agent edits, the host builds.**

How it connects:
- This server listens on `http://0.0.0.0:<port>/mcp` on the host.
- The agent container reaches it at `http://host.docker.internal:<port>/mcp`
  (harness already wrote that URL into the agent's MCP config when the user
  ran `harness mcp host-init`).
- Reachability is automatic. While this host MCP is enabled, harness maps
  `host.docker.internal` into the agent container and opens it through the
  firewall — it injects `--add-host=host.docker.internal:host-gateway` on Linux
  hosts (where Docker does not provide that name; on Docker Desktop / Windows it
  resolves on its own) and signals the firewall to allow it. No
  `harness net allow` and no manual `--add-host` are needed.

You do not need to wire any of that. It is done. You tailor `server.py` and
`project.json`.

## What to do

1. **Interview the user.** Ask only what you cannot discover by looking at the
   project. You need:
   - The absolute path to the project on the host (`project_dir`), e.g.
     `C:/dev/MyGame`. Confirm it exists if you can see it.
   - The build system. This template assumes **CMake**. If the project is a
     raw `.sln` / MSBuild project with no CMakeLists.txt, say so and adapt the
     tools to call `msbuild` instead of `cmake` (see "Adapting to MSBuild").
   - The CMake generator (default `Visual Studio 17 2022` for VS2022) and the
     default config (`Debug`/`Release`).
   - Which operations the agent actually needs. **Not all tools are needed.**
     A library that the agent only compiles needs `configure` + `build` +
     `get_build_errors`; drop `run_tests` if there are no tests, drop
     `run_target` if nothing should be executed. Fewer tools = less for the
     agent to misuse and a smaller tool list in its context.
   - Whether built executables should be runnable by the agent. If yes,
     populate the `targets` allowlist in `project.json` with the exact target
     names so the agent cannot run arbitrary binaries.

2. **Fill in `project.json`.** Replace the `C:/path/to/your/project`
   placeholder and set `generator`, `config`, and `targets`. Keep the port the
   user already chose at host-init time — it is baked into the agent's config;
   do not change it here without re-running host-init.

3. **Prune `server.py`.** Delete the `@mcp.tool()` functions the project does
   not need. Keep docstrings accurate — the agent reads them to decide when to
   call each tool. If the project has a build quirk (a bootstrap step, a code
   generator, a specific test runner), add a tool for it rather than making the
   agent guess.

4. **Verify the MCP SDK calls.** This template targets the `mcp` Python SDK
   (`from mcp.server.fastmcp import FastMCP`, `mcp.run(transport=
   "streamable-http")`). Run `pip show mcp` (or check `requirements.txt`) and
   confirm the constructor and `run()` calls match the installed version. If
   the API differs, fix it — do not guess.

5. **Get the user to launch it.** The server runs on the **host**, not in a
   container, and not by you (you are in the container). Tell the user to run
   `./run.sh` from this folder on the host. It is one git-bash-native script
   for every OS — on Windows it runs from the **same Git Bash the user runs
   `harness` in** (no PowerShell, no Developer prompt: CMake's
   "Visual Studio 17 2022" generator locates MSVC itself; just have `cmake` on
   PATH). It creates a venv, installs `requirements.txt`, and starts the server.
   The server must keep running while the agent works.

6. **Confirm the loop.** Once the server is up, the agent (in a normal
   `harness agent` session, not you) can call the build tools — reachability is
   automatic, no `harness net allow` needed. A good smoke test: have the user
   start an agent and ask it to call `configure` then `build`, and check the
   server's console shows the requests.

## Adapting to MSBuild (no CMake)

If the project is a `.sln`/`.vcxproj` with no CMake, replace the cmake calls
in `server.py`:
- `configure` → usually a no-op (or `nuget restore` / a bootstrap script).
- `build` → `msbuild <path-to.sln> /p:Configuration=<config> /p:Platform=x64
  [ /t:<target> ]`.
- `run_tests` → `vstest.console.exe <test.dll>` or the project's test runner.
Keep the `_run` helper (capture + truncate + exit code) as-is.

## Guardrails

- **Confirm before destructive steps.** `clean` deletes build output; that is
  fine. Do not add tools that delete source or run arbitrary shell strings from
  the agent — keep commands built from a fixed template plus validated args.
- **Keep the `targets` allowlist** if you expose `run_target`. An empty
  allowlist means "run any target," which lets the agent execute any built
  binary.
- **Do not fabricate.** If you are unsure whether a path, generator, or target
  name is right, ask the user or check the project files. A wrong path makes
  every build fail with a confusing error.
- **State what you changed.** When done, tell the user which tools you kept,
  what `project.json` now contains, and the exact command to launch the server.
