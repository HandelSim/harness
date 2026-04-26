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

## How harness handles line endings on Windows

The repo includes a `.gitattributes` file that forces LF line endings on shell scripts and config files, regardless of your `core.autocrlf` setting. You don't need to change git config.

If you encounter mysterious `\r: command not found` errors, run:

```bash
cd <install-root>
git rm --cached -r .
git reset --hard
```

This re-checks-out files with the .gitattributes rules applied. Should not be needed in practice.

## Performance notes

- Bind mounts to NTFS are slower than to Linux ext4. For performance-sensitive workloads, consider running harness inside WSL2 — but that's beyond the scope of this guide.
- Docker Desktop's default resource limits may be conservative. If `harness start` is slow or runs out of memory, increase Docker Desktop's CPU/RAM limits in its Settings.

## Limitations

- PowerShell and cmd are not supported. All harness commands must be run from Git Bash.
- File ownership tests (the `--user` flag, `chown` behavior) are non-meaningful on Windows because NTFS doesn't have POSIX UIDs. Files created in mounted volumes are owned by your Windows user automatically; the harness UID-remap logic that's necessary on Linux has no effect on Windows.
- Tmux is required for interactive agent TUIs. It ships with Git for Windows by default; verify with `which tmux`.

## Troubleshooting

- **"docker info" fails**: Docker Desktop is not running. Start it from the Start menu and wait 30-60 seconds for it to be ready. The installer and `harness preflight` will attempt to auto-start Docker Desktop, with up to 90 seconds of polling.
- **Bind mount errors**: ensure your install path is under `C:\Users\<you>\`. Network drives and OneDrive paths are unreliable.
- **`\r: command not found`**: line ending issue. Run `git checkout-index --force --all` to force reapplying .gitattributes rules.
- **`harness preflight` reports issues**: read each error carefully. Most issues are missing values in `.env` or hostnames missing from `.harness-allowlist`.
- **Auto-start times out**: 90 seconds is the default; on slower machines, start Docker Desktop manually and wait for the whale icon to settle before retrying.
