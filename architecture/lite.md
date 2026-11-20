# harness-lite (`lite/`)

One PowerShell file, `lite/harness-lite.ps1`, that does what default
`harness host` does (opencode + the proxy) with no docker, Python, node or
opencode: for locked-down Windows machines. It is a reimplementation, not a
wrapper, so it shares no code with the rest of the repo at run time. What ties
it to the repo is the build: the prompt texts it sends are rendered from the
real proxy, and a unit test fails when they drift.

## Files

| File | Kind | What |
|---|---|---|
| `src.ps1` | hand-written | Engine (agent loop, tools, prompt mode = port of the proxy's hybrid mode, native mode, config, console UI) and the GUI bridge. |
| `ui.html` | hand-written, ASCII | The GUI page (CSS + JS, no external resources). |
| `opencode-texts.json` | captured | opencode's own texts for the pinned version: system prompt per model family, tool definitions, tool sets per agent, the built-in skill. Recaptured only on an opencode version bump (see below). |
| `gen_texts.py` | build | `opencode-texts.json` + the real `proxy/` -> `texts.ps1`. |
| `build.py` | build | Runs `gen_texts`, then `src.ps1` + `texts.ps1` + `ui.html` -> `harness-lite.ps1`. |
| `texts.ps1` | generated, committed | `$script:TX[...]` here-strings. |
| `harness-lite.ps1` | built, committed | The file users copy and run. |
| `harness-lite.json` | gitignored | The user's API URL, key (plaintext), model. Written beside the script at run time. |

## Build invariants

- `gen_texts.py` imports `proxy/proxy.py` and calls its own functions
  (`translate_history_and_apply_prompt`, `_format_tool_entries`, `_slim_tools`,
  `_slim_prompt`, `_setup_reminder_template`, ...) on opencode's tool
  definitions, in host run mode with require-tool on. It reads the tracked
  defaults only: `INSTALL_ROOT` and every `PROXY_*` / `HARNESS_*` / `OAI_*` /
  `CHATGPT_*` variable is dropped before the import, so a user's edited
  `reminder.md` never leaks into the build. Inputs outside `lite/`:
  `proxy/proxy.py`, `proxy/reminder.md`, `proxy/tool-guidance.json`,
  `agents/slim-prompt.md`, `agents/slim-subagent-prompt.md`.
- flask and requests are stubbed when absent (CI's unit job installs nothing).
- `src.ps1` holds two placeholders, each exactly once: `#@@TEXTS@@` and
  `#@@UI@@`, the latter as the only line of `$script:UiPage = @'` ... `'@`.
  No line of `ui.html` may start with `'@`.
- Output is ASCII (non-ASCII text is written `<%XXXX%>` and decoded by `T` at
  load; Windows PowerShell 5.1 reads a BOM-less file as ANSI) with CRLF line
  ends, and has no timestamps: same inputs, same bytes.
- `tests/unit_lite_test.sh` (docker-free, in `harness test unit`) rebuilds into
  a temp dir and `cmp`s against the committed `texts.ps1` and
  `harness-lite.ps1`. **A change to the proxy's prompt texts therefore needs
  `python3 lite/build.py` in the same commit.** With `pwsh` on PATH the test
  also parse-checks the built file.

`texts.ps1` covers the proxy's *texts*. The proxy's *logic* (history
translation, tool-call extraction, require-tool, retries) is ported by hand in
`src.ps1`; a logic change in `proxy.py` is not detected by the test and must be
ported deliberately.

## Run-time shape

```
main thread: engine (all state)  <-- two queues -->  background runspace: HTTP/SSE server
                                                      on 127.0.0.1:<free port>, token-checked
                                                           ^
                                              Edge/Chrome --app window (ui.html)
```

- UI choice: GUI when the session is FullLanguage, on Windows and interactive;
  else the console UI. `-Gui` / `-Tui` force one; any GUI start failure, or no
  page connecting within 30 s, falls back to the console UI. Closing the page
  (no event-stream client for 10 s) saves and exits.
- Bridge: `GET /?t=<token>` (page, with a per-run CSP nonce), `GET /events`
  (SSE, replay with `since=N`), `POST /send` (`X-Token` header). Each turn runs
  as a nested pipeline that the server thread stops on `interrupt`; unanswered
  tool calls then get `Tool execution aborted` results. Test hooks:
  `HARNESS_LITE_GUI_NOOPEN=1` (print the URL, open nothing),
  `HARNESS_LITE_GUI_PORT`. The engine must keep running in
  ConstrainedLanguage mode, where only the console UI exists.
- The GUI is a front end on the same engine: in GUI mode the output functions
  are redefined to emit JSON events; the functions that build upstream requests
  are the same in both modes.
- Config: `harness-lite.json` beside the script (or `HARNESS_LITE_CONFIG`);
  per field switch > file > env > ask; validated with `GET /v1/models`; on failure (at start, or a
  401/403 mid-session) the request and error are shown and the values are asked
  for again.

## opencode version bump

`opencode-texts.json` is distilled from raw request captures of the pinned
opencode (one per model family, against a mock upstream). The capture script is
not in the repo; when the pin in `harness` changes, recapture, replace the
JSON, rebuild.
