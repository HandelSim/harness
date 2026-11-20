# harness-lite

`harness-lite.ps1` is `harness host` in one PowerShell file, for a Windows
machine where docker, Python and opencode are not an option. It reimplements
opencode's agent loop and tools and the harness proxy's request shaping, and
sends upstream what default `harness host` sends. Windows PowerShell 5.1 or
pwsh 7; nothing to install.

## Run

Copy `harness-lite.ps1` anywhere (it needs no other file), then from the
project folder:

```
powershell -ExecutionPolicy Bypass -File C:\path\to\harness-lite.ps1
```

- With no switch it opens the GUI (an opencode-style window: a local page
  served on 127.0.0.1 and shown in Edge/Chrome app mode) when it can, and the
  console UI otherwise. `-Gui` / `-Tui` force one. The GUI needs FullLanguage
  mode; in ConstrainedLanguage mode, or if the window cannot start, it says
  why and uses the console UI.
- `-Prompt "..."` runs one task and exits; `-Resume` continues the last
  conversation in this folder; `-Model`, `-BaseUrl`, `-AutoApprove` as in the
  header of the script.

## Config

The first run asks for the API URL, the API key and a model, checks them
against `GET <url>/v1/models`, and saves them in `harness-lite.json` beside the
script. Later runs load that file. If the saved or typed values fail, it shows
the request and the error and asks again, then overwrites the file.

```json
{ "api_url": "https://host/path", "api_key": "...", "model": "gpt-x-withtools", "auth": "bearer" }
```

Per field: switch > file > environment (`HARNESS_API_URL`, `HARNESS_API_KEY`,
`HARNESS_MODEL`, `HARNESS_AUTH`) > ask. A failure mid-session (HTTP 401/403)
asks again too and retries the turn. `HARNESS_LITE_CONFIG` points at
a different file.

**The key is stored in plaintext.** Anyone who can read the folder can read
it. `lite/harness-lite.json` is gitignored here; if you copy the script
elsewhere, keep its folder out of shared or synced locations and out of other
repos, and do not put the script inside the project you point the agent at
(its tools could read the key).

## GUI

An opencode look-alike: session view, tool blocks, diffs, todos sidebar,
permission prompt (once / always / reject, or type an answer), `esc esc` to
interrupt, `ctrl+p` command palette, `/` completion, model picker, config form.
Commands: `/help /new /models /undo /sessions /resume /export /todos /auto
/exit`. Not there (yet) versus opencode: Plan agent (build only), `/compact`,
`/redo`, `/init`, `@` file completion, `!` shell mode, token streaming,
subagent child view, themes, share, LSP, MCP.

## Rebuild

`harness-lite.ps1` and `texts.ps1` are generated and committed. After editing
`src.ps1` or `ui.html`, or after any change to the proxy's prompts
(`proxy/proxy.py`, `proxy/reminder.md`, `proxy/tool-guidance.json`,
`agents/slim-*.md`):

```
python3 lite/build.py
bash tests/unit_lite_test.sh
```

and commit the result. The unit test (part of `harness test unit`) fails while
the committed files differ from a fresh build. See `architecture/lite.md`.
