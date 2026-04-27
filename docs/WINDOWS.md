# Running harness on Windows

harness supports Windows via Git Bash. PowerShell and cmd are not supported.

## Requirements

- **Git for Windows 2.40+** — install from https://gitforwindows.org. Provides Git Bash, the only supported shell on Windows.
- **Docker Desktop for Windows with WSL2 backend** — recommended over Hyper-V backend. Install from https://docker.com.
- **A clone path under `C:\Users\<you>\`** — avoid OneDrive paths, network shares, and paths with spaces. Bind mount performance suffers on those.

## Setup

1. Install Docker Desktop and ensure it starts at least once successfully.
2. Install Git for Windows. Default options are fine; do NOT need to change `core.autocrlf` settings.
3. Open Git Bash. Confirm `docker info` runs successfully (Docker Desktop must be running).
4. cd to a working directory under `/c/Users/<you>/`.
5. Clone the harness repo: `git clone https://github.com/HandelSim/harness`
6. `cd harness`
7. Run `bash harness-install.sh` and follow prompts.
8. Edit `<install-root>/.env` and set required values (especially `PROXY_API_KEY`).
9. Open a new Git Bash session if PATH was modified by the installer.
10. Run `harness preflight` to validate configuration.
11. Run `harness start`.

## How harness handles paths on Windows

Git Bash on Windows uses MSYS, which auto-converts UNIX-style paths in
arguments when calling native Windows binaries like `docker.exe`. This is
useful for host-side paths (`/c/Users/foo/file` → `C:\Users\foo\file` so
Docker can find them) but breaks for *container-internal* arguments like
`--entrypoint /bin/bash` (which gets mangled to
`C:/Program Files/Git/usr/bin/bash`, a host path that has nothing to do
with the container).

harness handles this in two ways, both via helpers in
`scripts/lib/platform.sh`:

1. **`harness_docker` wraps `docker` invocations with `MSYS_NO_PATHCONV=1`
   on Windows.** This tells MSYS to skip path translation for that one
   call, so container-internal paths like `/bin/bash`, `/etc/harness/allowlist`,
   or `/workspace` reach Docker untouched. On Linux/macOS the wrapper is
   a transparent passthrough — same call, no env var.

2. **`harness_docker_path` normalizes host paths for bind-mount sources.**
   Docker Desktop's WSL2 backend reliably handles `C:/Users/...` form
   paths. Raw `/tmp/...` (a Git Bash MSYS-only mount) is not visible
   from outside Git Bash and bind mounts of those paths silently produce
   empty mounts. The helper uses `cygpath -m` to convert any host path
   to the canonical mixed-form Windows path. On Linux/macOS it's a
   passthrough.

Inside the harness CLI and test scripts, `-v "$src:/dst"` is always
written `-v "$(harness_docker_path $src):/dst"` so the host side is
normalized; the container side is left alone (and `harness_docker` keeps
MSYS from rewriting it).

A third helper, `harness_docker_exec`, exists for sites that previously
called `exec docker ...` (run_agent_print, attach paths, the
ccstatusline configurator). It preserves exec semantics while applying
the Windows env-var wrap, since shell functions cannot themselves be
exec'd.

## jq compatibility

The harness upgrade machinery uses `jq` to merge JSON config files
(envfile_merge, json_merge). Earlier versions used bash process
substitution — `<(printf '%s\n' "$merged")` — to feed jq the in-memory
merged result, but the **Windows-native `jq.exe`** (e.g., from
Chocolatey, Scoop, or downloaded direct from github.com/jqlang) cannot
read the MSYS-style `/proc/<pid>/fd/<n>` paths that `<(...)` produces.
The MSYS-native jq (installed via `pacman -S jq` in Git Bash) handles
them correctly, but most users only have the native jq.exe installed.

Phase 12 refactored every such site to use a real temp file instead.
The temp-file form works on every platform with no jq variant
distinction. If you encounter a jq error involving `/proc/...` paths in
older harness versions, the workaround is:

```bash
pacman -S jq    # install MSYS-native jq into Git Bash
```

## How harness handles line endings on Windows

The repo includes a `.gitattributes` file that forces LF line endings on shell scripts and config files, regardless of your `core.autocrlf` setting. You don't need to change git config.

If you encounter mysterious `\r: command not found` errors, run:

```bash
cd <install-root>
git rm --cached -r .
git reset --hard
```

This re-checks-out files with the .gitattributes rules applied. Should not be needed in practice.

## Terminal font for Unicode rendering

Claude-code and opencode use Unicode box-drawing and indicator characters
in their TUI. Git Bash's default font (Lucida Console) has limited Unicode
coverage. If you see underscores instead of box characters or icons, switch
to a font with broader Unicode coverage:

1. Right-click the MinTTY title bar
2. Options → Text → Font → Select
3. Pick "Cascadia Code", "Cascadia Mono", or "DejaVu Sans Mono"
4. Apply

The locale inside agent containers is set to C.UTF-8, so the rendering
capability is there — only the host terminal font choice limits what
displays correctly.

## Performance notes

- Bind mounts to NTFS are slower than to Linux ext4. For performance-sensitive workloads, consider running harness inside WSL2 — but that's beyond the scope of this guide.
- Docker Desktop's default resource limits may be conservative. If `harness start` is slow or runs out of memory, increase Docker Desktop's CPU/RAM limits in its Settings.

## Limitations

- PowerShell and cmd are not supported. All harness commands must be run from Git Bash.
- File ownership tests (the `--user` flag, `chown` behavior) are non-meaningful on Windows because NTFS doesn't have POSIX UIDs. Files created in mounted volumes are owned by your Windows user automatically; the harness UID-remap logic that's necessary on Linux has no effect on Windows.
- Tmux is not used anywhere in the harness runtime. Phase 18 dropped the wrapping from agent launch and Phase 19 removed the dead-code helpers and the test driver.

## OUTPUT_DIR (proxy debug dumps)

The proxy supports an optional `OUTPUT_DIR` env var that captures every
request/response trio for debugging. On Linux/macOS, set it to a path like
`/output` in `.env`.

On Windows under Git Bash, the leading slash gets auto-translated to a Git
Bash root prefix (`C:/Program Files/Git/output`). Use a double-slash to
bypass MSYS path translation:

```
OUTPUT_DIR=//output
```

Both forms bind-mount to the same host directory at
`<install-root>/state/output/`.

## Troubleshooting

- **"docker info" fails**: Docker Desktop is not running. Start it from the Start menu and wait 30-60 seconds for it to be ready. The installer and `harness preflight` will attempt to auto-start Docker Desktop, with up to 90 seconds of polling.
- **Bind mount errors**: ensure your install path is under `C:\Users\<you>\`. Network drives and OneDrive paths are unreliable.
- **`\r: command not found`**: line ending issue. Run `git checkout-index --force --all` to force reapplying .gitattributes rules.
- **`harness preflight` reports issues**: read each error carefully. Most issues are missing values in `.env` or hostnames missing from `.harness-allowlist`.
- **Auto-start times out**: 90 seconds is the default; on slower machines, start Docker Desktop manually and wait for the whale icon to settle before retrying.
