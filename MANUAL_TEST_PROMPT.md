# Manual End-to-End Test Prompt for harness

> Paste the contents below into a fresh Claude Code session running on a
> machine with the harness already installed. Provide your real upstream
> API credentials in `<install-root>/.env` before running. The agent will
> work through scenarios that automated tests can't cover (real LLM
> responses, subjective UX, multi-session resilience).
>
> Automated coverage lives in `scripts/full_pipeline_test.sh`; that script
> uses a mock upstream and validates the wiring. This document validates
> the things the wiring is wired to.

---

You are validating the "harness" project end-to-end against a real upstream
LLM API. Your job is to execute a sequence of scenarios and report findings.
Do not write code or modify the project — only run commands and observe.

For every scenario, capture:

- the exact command(s) you ran
- the exit code
- a representative excerpt of the output (truncate verbose TUI output to
  the first and last 20 lines if it's long)
- a one-sentence judgment: did the scenario pass cleanly, pass with warts,
  or fail?

If a scenario fails, **stop and report**. The scenarios depend on the
infrastructure remaining healthy.

## Setup verification

1. Run `harness doctor`. Report any errors or warnings. If errors, stop and
   report; the rest cannot proceed.
2. If `[runtime]` shows services aren't running, run `harness start` and
   wait for it to return. Re-run `harness doctor` and confirm `[runtime]`
   now shows ollama and proxy as healthy.
3. Confirm `harness list` reports zero agents (`no harness agents running`).

## Scenario A: Claude single-prompt sanity

```
harness claude -p "What is 2+2? Reply with just the number."
```

**Expected**: response containing "4". Report:
- exact output (full, no truncation — should be short)
- rough latency (count seconds with a stopwatch; first-token vs. completion)
- whether the response is sensible

## Scenario B: Claude TUI session and detach/reattach

In one terminal:

```
mkdir -p /tmp/harness-manual-B && cd /tmp/harness-manual-B
harness claude
```

Inside the TUI, send the prompt:

> List the files in this directory and tell me one observation about it.

Wait for the response. Then:

1. Press `Ctrl-b d` to detach from tmux. You should land back at your host
   shell. Verify with `harness list` — the agent should still appear.
2. Run `harness attach`. Verify the picker appears (or auto-attaches if only
   one agent), and that you reconnect to the same session with full
   scrollback intact.
3. Inside the TUI, send a follow-up prompt that requires the prior context:

   > What was the file you found most interesting and why?

   Verify the agent answers based on the earlier listing.
4. Detach again with `Ctrl-b d`. Run `harness stop` and confirm the agent
   is terminated.

Report the response quality, whether scrollback survived detach/reattach,
and whether the picker behaved sensibly.

## Scenario C: Opencode equivalent of A and B

Single-prompt:

```
harness opencode -p "What is 2+2? Reply with just the number."
```

Then a TUI session in `/tmp/harness-manual-C`:

```
mkdir -p /tmp/harness-manual-C && cd /tmp/harness-manual-C
harness opencode
```

Same shape as Scenario B: send a prompt, detach with `Ctrl-b d`, reattach
via `harness attach`, send a follow-up, then `harness stop`.

Note any opencode-specific behaviors (different keybindings, different
status indicators, slower or faster response). Some opencode versions
require provider-auth setup; if `opencode -p` fails with an auth/login
error, note it as a known limitation and continue with the TUI test.

## Scenario D: File creation with --yolo

From a temporary working directory:

```
mkdir -p /tmp/harness-manual-D && cd /tmp/harness-manual-D
harness claude --yolo -p "Create a file called test.py containing a function 'add(a, b)' that returns a+b. Then create test_test.py that contains a unittest.TestCase exercising add for at least three input pairs including a negative."
```

Verify after the agent completes:

1. Both files exist:
   ```
   ls -la test.py test_test.py
   ```
2. Ownership matches the host UID/GID (this is the file-ownership
   round-trip the proxy is responsible for):
   ```
   stat -c '%u %g' test.py test_test.py
   id -u; id -g
   ```
   The first column should match `id -u`, the second `id -g`.
3. The tests pass:
   ```
   python -m unittest test_test
   ```

Report the exact contents of `test.py` and `test_test.py`, and whether
the tests pass.

## Scenario E: Multi-agent coexistence

Open two separate terminals.

- Terminal 1: `cd /tmp/harness-manual-E1 && mkdir -p . && harness claude`
- Terminal 2: `cd /tmp/harness-manual-E2 && mkdir -p . && harness opencode`

(Create the directories first if they don't exist.)

In a third terminal:

```
harness list
```

Both agents should appear with different `MOUNT` values.

In each TUI, send a unique prompt that mentions a unique landmark
("write three sentences about Mount Fuji" in one, "write three sentences
about Lake Baikal" in the other). Verify outputs do not cross-contaminate
— the Fuji terminal must not produce Baikal text and vice versa.

Detach both, then `harness stop` each by name (use names from
`harness list`).

Report whether the two agents coexisted cleanly, whether `harness list`
distinguished them, and whether outputs stayed in their respective
sessions.

## Scenario F: Stop with picker

Bring up at least two agents (e.g. repeat Scenario E setup). Detach from
both so you're back at the host shell. Run:

```
harness stop
```

(no name argument). Verify the picker:
- prints a numbered list of running agents with tool + mount,
- prompts for a selection,
- accepts a numeric choice and stops the chosen one,
- leaves any non-selected agent running.

Confirm with `harness list` that exactly one of the agents is gone.
Then `harness stop` the other and confirm `harness list` is empty.

## Scenario G: Update flow

```
harness update
```

Expected: either "Already up to date." or a clean fast-forward pull.
Report what you saw.

```
time harness upgrade
```

Expected: pull (no-op or fast-forward), rebuild (cached layers should make
this fast), restart of services. Report the wall time and whether services
came back healthy (`harness doctor` after upgrade should still show
ollama/proxy healthy).

## Scenario H: Recovery (interrupt mid-stream)

```
mkdir -p /tmp/harness-manual-H && cd /tmp/harness-manual-H
harness claude
```

Send a prompt that produces a long response:

> Write a 500-word essay on the cultural significance of toast.

When you see text actively streaming into the pane, press `Ctrl-C`. The
TUI should remain alive (claude itself handles the SIGINT and prompts you
again; tmux is unaffected).

Verify by sending a follow-up:

> What's 7 times 8?

Expected: a normal response. The session is still healthy.

Detach with `Ctrl-b d`, reattach with `harness attach`, and verify the
scrollback shows both the interrupted essay and the follow-up exchange.
Then `harness stop`.

Report whether interrupt + follow-up worked cleanly, and whether
scrollback survived the round trip.

## Scenario I: Configuration failure mode

This scenario validates that misconfiguration produces a clear error
rather than a hang or cryptic crash.

1. `harness down`
2. Edit `<install-root>/.env`: comment out `PROXY_API_KEY` or rename it to
   `PROXY_API_KEY_BROKEN` so the variable is unset when compose loads it.
3. `harness start` — should still come up; the proxy doesn't validate the
   key on boot.
4. `harness claude -p "ping"` — expected: a non-zero exit with an error
   that mentions the upstream / API key / authorization, NOT a hang and
   NOT a stack trace from inside claude itself.
5. Restore the original line in `.env`.
6. `harness start` to pick up the corrected env, then re-run
   `harness claude -p "ping"` and verify it now succeeds.

Report the error you saw in step 4 — verbatim. The quality of that error
message is the thing we're testing here.

## Scenario J: Skill persistence

> **Automated baseline:** `HARNESS_RUN_SLOW=1 bash scripts/integration_test.sh`
> covers Phases 3.1–3.11 of this scenario non-interactively (pipx install of
> graphifyy, host-bind-mount visibility, `graphify install` skill registration,
> `graphify update .` against the test-project fixture, file-ownership UID match,
> and persistence across a fresh container). Run that first; the steps below
> add interactive coverage and exercise additional pipx packages.

This scenario validates that things installed inside an agent survive
container restarts and full service rebuilds. Persistence is implemented
by bind-mounting `<install-root>/state/agent/<tool>/` as the agent's whole
`/home/harness` and seeding the build-time skeleton from `/etc/skel/harness/`
on first run.

1. ```
   harness claude
   ```
   In the TUI, drop into a shell (claude's `!` shell escape) and run:
   ```
   pipx install graphifyy
   ```
   (or any pipx-installable package — `cowsay` works as a small smoke test).
2. Confirm the install landed under the persistent home:
   ```
   which graphify
   ls ~/.local/bin
   ```
   The binary should be in `~/.local/bin/`.
3. Exit the agent (`/exit`) and verify on the host that the file
   appeared in the mount:
   ```
   ls "$(harness doctor 2>/dev/null | grep 'install root' | awk '{print $NF}')/state/agent/claude/.local/bin"
   ```
4. Re-launch: `harness claude`. Inside the TUI, `! which graphify` should
   still resolve. (No re-install required — the binary was on disk all
   along.)
5. Tear down and bring back up: `harness down && harness start`. Repeat
   step 4. graphify must still work, since the home dir is bind-mounted
   from outside the container lifecycle.
6. Verify the marker file exists:
   ```
   ls -la <install-root>/state/agent/claude/.harness-home-initialized
   ```

Report whether each step passed cleanly, and the contents of the marker
check in step 6.

## Scenario K: Serena MCP

> **Automated baseline:** `HARNESS_RUN_SLOW=1 bash scripts/integration_test.sh`
> covers Phase 2: install → restart → reachability on tcp://serena:9121 →
> agent MCP-config side-file merge → workspace mount visibility → TUI
> tool-call rendering → down/up cycle → disable/enable → uninstall. Run
> that first; the steps below add interactive coverage against a real
> upstream and your own project.

This scenario exercises the MCP registry with the heavyweight Serena
service. It is not in CI because the build is slow; treat it as a
release-time validation.

1. Install Serena and bring services up:
   ```
   harness mcp install serena
   harness start
   ```
   Expected: a long build (~5–10 minutes the first time) followed by
   `harness mcp list` showing serena with state `installed-enabled`. If
   the build fails, capture the log; that's a Serena-side issue worth
   reporting upstream.
2. (Optional) Set `HARNESS_PROJECTS_ROOT` in `<install-root>/.env` to a
   single project directory you want Serena to index, then re-run
   `harness start` to apply. Without this, Serena gets read access to
   `/home`.
3. Confirm Serena is healthy:
   ```
   harness doctor
   ```
   Look for an `[mcp]` section reporting `serena  running (healthy)`.
4. Launch an agent in a real project and ask it to use Serena:
   ```
   cd $HOME/some-test-project
   harness claude
   ```
   Inside the TUI, prompt:
   > Use the serena MCP tool to list the symbols in this project's
   > main entry point.

   Expected: claude calls Serena's tools and surfaces useful output
   (symbol names, locations). If claude says it doesn't have an MCP
   tool available, drop into a shell and run `claude mcp list` —
   `serena` should be present.
5. Toggle disable / enable without uninstalling. Disable just flips the
   enabled flag — the entry stays installed and `mcp up` can still bring
   it back manually:
   ```
   harness mcp disable serena
   harness mcp status serena      # state: installed-disabled, enabled: false
   harness mcp up serena          # manually start, even though disabled
   harness mcp down serena
   harness mcp enable serena      # back to installed-enabled
   ```
6. Uninstall and verify cleanup preserves the data dir:
   ```
   harness mcp uninstall serena --force
   ls <install-root>/state/mcp/serena/
   ```
   Only `data/` should remain. `compose.yml`, `client-config.json`, and
   `harness-meta.json` are gone.
7. Re-install and verify the persistent index is reused:
   ```
   harness mcp install serena
   harness start
   ```
   First start after re-install should be fast (image cached). The
   data dir is still there.

Report the latency of the first build, the latency of subsequent starts,
the quality of Serena's responses inside claude, and whether the
uninstall/re-install cycle preserved data.

## Scenario L: Network firewall + bypass controls

This scenario validates the universal egress firewall and the user-facing
controls added in Phase B2.

1. Inspect the seed allowlist:
   ```
   harness net list
   ```
   Expected: a table with at least DNS-related defaults and any hosts
   shipped in `.harness-allowlist.example`. **`api.anthropic.com` should
   NOT appear** — the cosmetic warning at agent startup is intentional.

2. Run `harness doctor`. Confirm the `[network]` section reports:
   - the allowlist path,
   - a host count,
   - whether the host derived from `PROXY_API_URL` is in the allowlist
     (it should be, modulo edge cases like CDN-fronted upstreams).

3. Add a host with the round-trip:
   ```
   harness net allow github.com --git-push
   harness net list             # github.com appears with mode 'push'
   harness net deny github.com
   harness net list             # github.com gone
   ```
   Try an invalid host — the script should refuse and exit non-zero:
   ```
   harness net allow 'no spaces allowed.com'
   harness net allow 'https://github.com'
   ```

4. Verify the per-launch bypass:
   ```
   mkdir -p /tmp/harness-manual-L && cd /tmp/harness-manual-L
   harness claude --net -p "Use bash to run: curl -sS -o /dev/null -w '%{http_code}\n' https://example.com"
   ```
   Expected: stderr from `harness` itself includes a loud `--net` warning;
   the curl returns `200` (or any 2xx/3xx, just not a connection refused).
   Without `--net`, the same request would fail unless `example.com` is on
   the allowlist.

5. Service-level open/close:
   ```
   harness net open proxy            # type the phrase when prompted
   harness net status                # 'proxy' listed under open services
   harness restart
   harness doctor                    # [network] reports proxy as overridden
   harness net close proxy
   harness restart
   ```
   The phrase prompt must reject anything other than the literal
   `I understand the risks`.

Report:
- whether the listing/round-trip is intuitive,
- whether the typed-phrase guard works as advertised,
- whether `--net`'s stderr warning is loud enough that you'd notice it,
- any hosts you had to add to make daily work usable (worth feeding back
  into the seed allowlist).

## Scenario M: Status line customization

1. Launch claude in any working directory:
   ```
   harness claude
   ```
   The bottom status line should show the model name, the current
   directory (with home abbreviated), the git branch (if any), and a
   context-bar progress indicator.

2. Exit the agent. Run:
   ```
   harness claude-statusline-config
   ```
   The ccstatusline TUI should appear. Make a visible change (e.g.,
   recolor a widget) and save/quit.

3. Re-launch `harness claude` and confirm the change is reflected. Also
   confirm:
   ```
   ls "$(harness doctor 2>/dev/null | grep 'install root' | awk '{print $NF}')/agent/claude/.config/ccstatusline/settings.json"
   ```
   exists. Settings persist across container rebuilds (bind-mounted home).

Report whether the configurator launched cleanly without requiring
ollama/proxy services, and whether your edits stuck.

## Scenario K2: MCP lifecycle deprecation aliases

For backward compatibility with Phase 6 muscle memory, the old verbs
still work but emit a `DEPRECATED` warning. Verify them quickly:

1. Pick any registry MCP that's currently `available`:
   ```
   harness mcp enable <name>
   ```
   Expected: stderr line starting with `DEPRECATED:`, then a normal
   install. `harness mcp list` should show `installed-enabled`.
2. Tear it down with the Phase 6 form:
   ```
   harness mcp disable <name> --force
   ```
   Expected: another `DEPRECATED:` warning, then a normal uninstall.

If either alias fails or stops emitting the warning, that's a regression
worth flagging.

## Scenario N: Upgrade flow against a synthetic version transition

This scenario validates that `harness upgrade` propagates new env variables
introduced in a newer version of the repo into the user's existing `.env`
without touching their values. Adapt the same pattern to other config
files if you want to be thorough.

1. Snapshot your current `.env`:
   ```
   cp <install-root>/.env <install-root>/.env.before-upgrade
   diff -q <install-root>/.env <install-root>/.env.before-upgrade   # should match
   ```

2. Inside the harness clone, edit `.env.example` to introduce a new
   variable. Pick a name unlikely to collide with anything real:
   ```
   cd <install-root>/harness
   printf '\n# Demo: Phase B3 manual test variable.\nHARNESS_TEST_NEW_VAR=test_default\n' >> .env.example
   git add .env.example
   git commit -m 'manual test: new env var (will revert)'
   ```

3. Preview the upgrade. With `--check` no files should change:
   ```
   harness upgrade --check
   ```
   Expected: the preview lists `env_vars`, and the `[env_vars] envfile_merge`
   line reports it would add `HARNESS_TEST_NEW_VAR`. Re-run `diff -q` from
   step 1 to confirm `.env` is byte-identical.

4. Apply the upgrade non-interactively (skip the daemon dance):
   ```
   harness upgrade --no-prompt --no-restart
   ```
   Expected output: `Upgrade summary:` with `[env_vars] envfile_merge: 1
   change(s)`.

5. Verify `.env` now contains the new variable with the default and a
   marker comment, but every prior value is unchanged:
   ```
   grep HARNESS_TEST_NEW_VAR <install-root>/.env       # → HARNESS_TEST_NEW_VAR=test_default
   grep 'Added by harness upgrade' <install-root>/.env # → marker comment present
   diff <install-root>/.env.before-upgrade <install-root>/.env | head -40
   ```
   The diff should show only insertions (no deletions or in-place edits).

6. Customize the new variable, then re-run upgrade — your value must win:
   ```
   sed -i 's|HARNESS_TEST_NEW_VAR=test_default|HARNESS_TEST_NEW_VAR=custom_value|' \
       <install-root>/.env
   harness upgrade --no-prompt --no-restart
   grep HARNESS_TEST_NEW_VAR <install-root>/.env       # → still custom_value
   ```
   Expected: the second run reports `[env_vars] envfile_merge: 0 change(s)`
   and your `custom_value` is preserved.

7. Cleanup:
   ```
   cd <install-root>/harness
   git reset --hard HEAD~1                                              # drop the demo commit
   sed -i '/^HARNESS_TEST_NEW_VAR=/d' <install-root>/.env
   sed -i '/Added by harness upgrade/d' <install-root>/.env
   rm <install-root>/.env.before-upgrade
   ```

Report whether the dry-run preview matched the apply-mode result, whether
existing values were preserved on both runs, and whether the marker comment
was clear.

## Final report

Summarize at the end:

- **Pass cleanly**: scenarios that worked exactly as expected
- **Pass with warts**: scenarios that worked but had rough edges (slow
  responses, ugly output, race conditions, awkward UX)
- **Fail**: scenarios that did not work — include reproduction details
- **Subjective**: a few sentences on overall TUI quality, response
  latency feel, and whether you would trust this harness for daily use

Do not paper over problems. If something is broken or feels broken,
say so.
