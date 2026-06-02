# Codebase Concerns

**Analysis Date:** 2026-06-02

Scope: the current post-migration `harness` codebase on branch `nollama`.
ollama is fully removed from the data path (no ollama service in
`docker-compose.yml`, no `ollama/` image dir, no `/api/chat`/NDJSON path).
opencode talks directly to the Flask proxy over `/v1/chat/completions`.
The concerns below are the live ones for that topology, not the migration.

## Tech Debt

**`proxy/proxy.py` is a single-file monolith:**
- Issue: All proxy behavior (config read, prompt builders, tool-call
  extraction, SSE/JSON emission, debug dumps, Flask routes, token
  estimation) lives in one module.
- Files: `proxy/proxy.py` (1870 lines, confirmed `wc -l`).
- Impact: Every proxy change touches one large file; the cooperative-prompt
  builders, the wire translation, and the Flask plumbing have no module
  boundary, so unrelated edits collide and the test file mirrors the same
  monolith. Architecture doc itself calls it "single-process, single file"
  (`architecture/proxy.md:14`).
- Fix approach: Split into modules behind the Flask app — e.g.
  `prompt_builders.py`, `toolcalls.py`, `wire.py` (SSE/JSON emit),
  `config.py`. Keep `proxy.py` as the route + orchestration layer. Do this
  only when a change forces it; a gratuitous split churns the prompt text
  that is deliberately tuned (`proxy/proxy.py:358-365`).

**`harness` CLI is a 5481-line bash monolith:**
- Issue: The entire management CLI (self-locate, env load, all `cmd_*`
  subcommands, compose wrapper, runtime override, agent launch, jq sidecar,
  doctor/preflight, net, mcp) is one bash file.
- Files: `harness` (5481 lines, confirmed `wc -l`). Architecture doc still
  says "~4200 lines" (`architecture/harness-cli.md:5`) — stale by ~1300
  lines, itself a doc-drift item.
- Impact: One file holds every code path; bash has no namespacing, so
  helper-name collisions and accidental global-var reuse are real risks.
  Hard to unit-test in isolation (the suite sources the whole file via
  `HARNESS_SOURCE_ONLY=1`). The line-count claim in the doc should be
  corrected.
- Fix approach: Continue migrating cohesive groups into `scripts/lib/`
  (the pattern already exists: `platform.sh`, `net_helpers.sh`,
  `upgrade_actions.sh`). MCP and doctor are the next candidates. Update the
  line-count note in `architecture/harness-cli.md:5` in the same commit as
  any structural change.

**`OLLAMA_CONTEXT_LENGTH` legacy alias survives as naming debt:**
- Issue: The env knob is `MODEL_CONTEXT_LENGTH`, but the proxy and agent
  entrypoint still read `OLLAMA_CONTEXT_LENGTH` as a fallback to avoid
  breaking pre-migration `.env` files. The dead vendor name persists in
  config, code, and docs.
- Files: `proxy/proxy.py:63-67` (the `or os.environ.get("OLLAMA_CONTEXT_LENGTH")`
  fallback), `agents/entrypoint.sh:138-139`
  (`${MODEL_CONTEXT_LENGTH:-${OLLAMA_CONTEXT_LENGTH:-200000}}`),
  `.env.example:67-68` (documents the alias), `architecture/proxy.md:546`,
  `tests/INVENTORY.md:190`, `tests/COVERAGE.md:284`.
- Impact: Misleading — implies an ollama dependency that no longer exists.
  Every reader of `.env.example` and the proxy config has to be told "this
  is just a legacy alias." Low functional risk, real comprehension cost.
- Fix approach: Keep the fallback for one more upgrade cycle (it is the only
  thing protecting existing `.env` files), then drop the
  `OLLAMA_CONTEXT_LENGTH` read once a `harness upgrade` has had time to
  rewrite installs to `MODEL_CONTEXT_LENGTH`. Track as a dated removal, not
  an indefinite carry.

**`_normalize_api_base` is duplicated between proxy and CLI with a manual sync rule:**
- Issue: The URL-base normalization (strip trailing `/v1/chat/completions`,
  `/chat/completions`, `/v1`) exists twice in two languages, with only a
  comment enforcing parity.
- Files: `proxy/proxy.py:72` (`def _normalize_api_base`) and `harness:933`
  (`_api_base()`). Both `harness:929-932` and `architecture/harness-cli.md:46`
  explicitly say "keep the two in sync."
- Impact: If one is changed (e.g. to also strip `/v1/`-with-trailing-slash or
  a new endpoint shape) and the other is not, the CLI auth/model probe and
  the proxy's actual request would derive different endpoints from the same
  `PROXY_API_URL` — the probe could pass while real requests 404, or vice
  versa. The drift is silent until a malformed base value hits it.
- Fix approach: There is no shared runtime between bash and Python here, so a
  literal shared function is hard. Mitigate with a parity test: a single
  fixture table of `(input, expected_base)` pairs asserted against both
  `_api_base` (sourced bash) and `_normalize_api_base` (imported Python).
  That converts "keep in sync" from a comment into a CI gate.

**Residual ollama references that are now dead or misleading:**
- Issue: After teardown, `ollama` still appears in a few tracked files.
- Files and dispositions:
  - `CLAUDE.md:43` — the architecture-router table still lists `ollama/` as a
    path that maps to `containers.md`. The dir no longer exists. **Under the
    do-not-modify guard** (CLAUDE.md is owner-only); flag for HandelSim, do
    not edit.
  - `.github/workflows/ci.yml:99,122,163,186,216,246,284` — buildx cache keys
    and comments still hash/reference `ollama/Dockerfile` and
    "proxy+ollama+agent image set." The `hashFiles('...ollama/Dockerfile')`
    entries are now no-ops (the path is gone, so it contributes nothing to
    the hash) but the comments are misleading. **Under the do-not-modify
    guard** (`.github/`); flag for owner.
  - `architecture/containers.md:31,79` — "there is no ollama hop" is correct
    and intentional (reassures the reader). Leave.
  - `.planning/PROJECT.md`, `.planning/ROADMAP.md`, `.planning/STATE.md` —
    heavy ollama references, but these are the GSD migration planning
    artifacts describing the completed milestone, not runtime. Out of scope
    for a code sweep; they document history by design.
- Impact: The two guarded files (`CLAUDE.md`, `ci.yml`) actively mislead a
  reader into thinking an ollama image still builds. Functionally inert.
- Fix approach: Owner (HandelSim) removes the stale `ollama/` row from
  `CLAUDE.md:43` and the `ollama/Dockerfile` tokens from the ci.yml cache
  keys/comments. Agents must not touch either file unguarded.

## Known Bugs / Fragile Areas

**Debug-dump directory (`state/output/`) is never rotated or pruned:**
- Issue: Every request writes 4-5 JSON files to `OUTPUT_DIR` and nothing
  ever deletes them.
- Files: `proxy/proxy.py:346-355` (`save_debug_file` — open-and-write only,
  no cleanup), call sites throughout `catch_all`. No rotation/prune logic
  exists anywhere (`grep` for rotate/cleanup/prune in `proxy.py` and
  `harness` finds none).
- Impact: On a long-lived install with dumps enabled, `state/output/`
  grows unbounded — one set per request, each containing the full
  conversation. Disk fills silently. This is the same disk-exhaustion class
  the project's own CLAUDE.md warns about for CI runners.
- Fix approach: Add a size- or age-based cap (e.g. keep the last N req_ids,
  or delete dumps older than M hours) on proxy startup and/or per-write.
  Dumps default to disabled (`OUTPUT_DIR` optional, `proxy.py:331-342`), so
  this only bites operators who turned them on — but those are exactly the
  long-debugging sessions where it bites hardest.

**opencode 1.15.x headless stdout race:**
- Issue: In `-p`/`--print` (non-TTY) mode, opencode's `run` renderer drops
  the assistant body (and on ~10% of fast runs the entire json stream,
  including `step_start`) when the session goes idle before the text event
  is processed.
- Files: `agents/entrypoint.sh:317-340` (the `HARNESS_PRINT_MODE` recovery
  branch), documented at `architecture/containers.md:123-152` and
  `agents/Dockerfile:73-81`.
- Impact: Without the workaround, `-p` output is empty on fast replies (mock
  upstream, cached models). This already forced an opencode rollback (issue
  #86) and a `full_pipeline_test` T10 failure.
- Fix approach: The current mitigation (run `--format json` to a temp file,
  read session id from `step_start` else fall back to newest persisted
  session via `opencode session list`, then `opencode export`) is in place
  and works. The fragility is that it is wholly contingent on opencode
  internals; revisit when opencode fixes the renderer (track
  anomalyco/opencode#20755 and the version note in `agents/Dockerfile`).

**`_pare_task_description` is fragile to an opencode header rename:**
- Issue: The hybrid prompt pares opencode's `task` tool description by
  anchoring on the literal header
  `Available agent types and the tools they have access to:`
  (`_OPENCODE_TASK_AGENTS_HEADER`).
- Files: `proxy/proxy.py:265-272` (header constant + parsing rationale),
  `architecture/proxy.md:274-288`.
- Impact: If a future opencode renames that header, the anchor misses. The
  design degrades gracefully (falls back to the **full** description — more
  tokens, never a silent loss of the agent list), so it is fragile but not
  data-losing. The canary is `proxy/test_proxy.py`
  `TestTaskDescriptionParing`.
- Fix approach: No action needed beyond keeping the canary green. If the
  fallback starts firing in practice (tokens balloon), update the header
  constant to match the new opencode string.

**opencode version coupling is spread across multiple files:**
- Issue: The pinned opencode version and its known-behavior workarounds are
  referenced in several places that must move together on a bump.
- Files: `agents/Dockerfile:103` (the `ARG OPENCODE_VERSION=1.15.7` source of
  truth) plus behavior notes/assumptions in `agents/Dockerfile:73-81`,
  `agents/entrypoint.sh:317-340`, `proxy/proxy.py:272` (header verified
  "1.14.41 and 1.15.7"), `architecture/proxy.md:283-284`,
  `architecture/containers.md:125-147`.
- Impact: A version bump that changes the renderer race, the json-stream
  behavior, or the `task` header requires re-verifying and updating all of
  these. The bump checklist lives only in the `OPENCODE_VERSION` comment in
  `agents/Dockerfile` — easy to miss the proxy-side coupling.
- Fix approach: Keep the bump checklist in `agents/Dockerfile` authoritative
  and have it explicitly enumerate the proxy-side header check
  (`proxy/proxy.py:272`) and the entrypoint print-mode branch. The
  `TestTaskDescriptionParing` and `full_pipeline_test` T10 canaries catch
  the two most likely regressions.

**The Flask development server runs the production listener:**
- Issue: The proxy is served by Flask's built-in `app.run()`, which is a
  single-threaded WSGI dev server not intended for production.
- Files: `proxy/proxy.py:1866` (`app.run(host=PROXY_HOST, port=PROXY_PORT,
  debug=False)`), `proxy/proxy.py:1588` (`app = Flask(__name__)`).
- Impact: `app.run` warns it is not for production. It is effectively
  single-request-at-a-time; one slow upstream call (up to `PROXY_TIMEOUT`,
  default 180s) blocks any concurrent agent request. For a
  single-user/single-agent harness this is tolerable; with multiple agents
  on one proxy it serializes them.
- Fix approach: Front with a real WSGI server (gunicorn/waitress) with a
  small worker count, or document the single-flight limitation explicitly.
  See Dependencies at Risk.

**`verify=False` on every upstream TLS call:**
- Issue: TLS certificate verification is disabled on all requests to the
  upstream.
- Files: `proxy/proxy.py:1616`, `proxy/proxy.py:1695` (chat + models calls),
  rationale at `proxy/proxy.py:45` and `architecture/proxy.md:38`.
- Impact: The upstream uses a self-signed cert, so `verify=True` would fail.
  But `verify=False` accepts any cert, so a network MITM between proxy and
  upstream is undetectable at the TLS layer. Mitigated in practice by the
  egress firewall pinning the allowed host, but not by certificate identity.
- Fix approach: Pin the upstream's specific self-signed cert (pass its CA/leaf
  to `verify=<path>`) instead of disabling verification wholesale. That keeps
  the self-signed cert working while restoring MITM detection.

## Security Considerations

**Disabled upstream TLS verification:**
- Risk: `verify=False` (see Fragile Areas above) means no cert-identity check
  on the proxy-to-upstream hop.
- Files: `proxy/proxy.py:1616,1695`.
- Current mitigation: Egress firewall restricts the proxy to the configured
  `PROXY_API_URL` host (`architecture/containers.md:213-215`).
- Recommendation: Pin the self-signed cert rather than disabling verification.

**Debug dumps contain full conversation content:**
- Risk: When `OUTPUT_DIR` is set, the proxy writes the entire inbound request
  and the translated upstream payload (all messages, tool results, file
  contents the agent read) to plaintext JSON on the host.
- Files: `proxy/proxy.py:1683` (`save_debug_file(req_id, "02", "API_Request",
  upstream_payload)` — `upstream_payload` is `{model, messages: translated}`),
  plus the `01_Inbound_Request` / `03_API_Response` / `04` dumps; sink at
  `proxy/proxy.py:346-355`; bind-mounted to `state/output/`
  (`architecture/README.md:50`, `architecture/proxy.md:567-579`).
- Current mitigation: Dumps default OFF (`OUTPUT_DIR` optional). Combined with
  the no-rotation issue above, when on they accumulate sensitive content
  indefinitely.
- Recommendation: Document that enabling dumps persists full conversation
  content (including any secrets the agent handled) to disk; add the rotation
  cap from the Known Bugs section so the exposure window is bounded.

**The API key is NOT written to debug dumps (good):**
- Note: The `02_API_Request` dump payload is `upstream_payload`
  (`proxy/proxy.py:1670-1683`), which is `{model, messages}` only. The
  `Authorization: Bearer {PROXY_API_KEY}` header is built separately at
  `proxy/proxy.py:1685-1688` and `1611` and is never passed to
  `save_debug_file`. So the upstream key does not land on disk in the dumps.
  Startup logging also redacts via `_redact_key` (`architecture/proxy.md:553`).
  This is the correct posture; flagged here so a future refactor that starts
  dumping headers is recognized as a regression.

**Inbound proxy auth is effectively unauthenticated:**
- Risk: opencode sends a `Bearer harness-dummy`-style header, but the proxy
  does not read or validate any inbound `Authorization` header — there is no
  `request.headers.get("Authorization")` check in `catch_all` (confirmed by
  grep). Anything that can reach `proxy:8000` on `harness-net` can drive the
  proxy and burn the real upstream key.
- Files: `proxy/proxy.py` `catch_all` (no inbound auth read); the only auth is
  the proxy-to-upstream `Bearer PROXY_API_KEY` at `proxy/proxy.py:1611,1687`.
- Current mitigation: The proxy listens only on the internal `harness-net`
  bridge, and the egress firewall constrains who is on that network. No host
  port is published for the proxy in `docker-compose.yml`.
- Recommendation: Acceptable given the network isolation (a single-tenant,
  internal bridge). If the proxy is ever exposed beyond `harness-net`, add a
  shared-secret check on the inbound header before trusting requests. Treat
  "proxy on a published port" as the trigger to add inbound auth.

**`HARNESS_FIREWALL_DISABLED` bypasses egress filtering:**
- Risk: Setting `HARNESS_FIREWALL_DISABLED=1` short-circuits
  `init-firewall.sh` and grants the container unrestricted egress.
- Files: firewall init referenced at `architecture/containers.md:208-213`;
  set by `harness net open <service>` (stamped into the runtime override) and
  `--net` per launch.
- Current mitigation: `harness net open` requires the operator to type
  `I understand the risks` on a TTY (`architecture/containers.md:228-230`);
  scripts cannot bypass. A loud bypass message is logged.
- Recommendation: This is an intentional, gated escape hatch — keep the
  human-in-the-loop confirmation. No change needed; documented so it is not
  mistaken for an accidental hole.

## Performance Bottlenecks

**No streaming from upstream — full buffer, then emit:**
- Problem: The proxy waits for the complete upstream response before it
  begins emitting SSE to opencode. There is no token-by-token passthrough.
- Files: `proxy/proxy.py` response-emission path; documented at
  `architecture/proxy.md:480-485` ("the proxy is NOT streaming from upstream
  — it gets the full response, then translates").
- Cause: Tool-call extraction needs the whole assistant message to scan for
  balanced ```json blocks (`extract_tool_calls_and_text`,
  `proxy/proxy.py` `_scan_balanced_json`), so it cannot emit deltas
  incrementally without re-architecting the parser to be streaming-aware.
- Improvement path: For pure-text turns (no tool calls), a streaming
  passthrough would cut time-to-first-token. But that requires detecting
  tool-call intent mid-stream, which the balanced-brace scan can't do
  half-buffered. Low priority: latency is "unchanged" per the doc because the
  upstream call dominates and memory cost is a few KB. Revisit only if
  perceived latency on long text replies becomes a complaint.

**`_estimate_tokens` uses a fixed `len(text) // 3` divisor:**
- Problem: Local token estimation hardcodes chars/3.
- Files: `proxy/proxy.py:1357-1361` (`n = max(1, len(text) // 3)`), call
  sites `proxy/proxy.py:1777,1780`. Rationale at `architecture/proxy.md:520-536`.
- Cause: Upstream `prompt_tokens` is unreliable (non-monotonic, sliding-window
  truncation), so the proxy estimates locally for a stable context bar.
  chars/3 is a deliberate tuning for code/JSON-dense agent turns (vs the
  chars/4 prose rule of thumb).
- Improvement path: The divisor is a heuristic, not a tokenizer. It will be
  wrong (high or low) for unusual content mixes (heavy CJK, base64 blobs).
  Accuracy is bounded by design — the goal is monotonic growth for
  auto-compaction, not exactness. Only worth replacing with a real BPE
  tokenizer if compaction visibly fires too early/late in practice; that adds
  a heavy dependency for marginal gain.

## Scaling Limits

**Single upstream key locks every ~8h and expires ~monthly, with no rotation:**
- Current capacity: One `PROXY_API_KEY` drives every request from every agent.
- Limit: The upstream locks the key every ~8 hours and expires it after
  ~1 month (`architecture/upstream-api.md:57-62`). A locked key returns `401`
  with an `unlock_url`; unlocking requires a signed-in **browser** session and
  cannot be automated by the harness (`architecture/upstream-api.md:64-84`).
- Scaling path: There is no key pool and no auto-rotation — by upstream design
  (unlock needs a human browser). The harness surfaces the lock cleanly (the
  CLI auth probe gates the launch and prints the clickable unlock URL —
  `architecture/harness-cli.md:46-54`; `harness unlock` exists), but recovery
  is manual. The only documented future option (pull a session key from a
  logged-in browser) is uncommitted. For now the system is inherently
  single-key, single-user, with periodic manual unlocks. Multi-user or
  always-on operation is not supported without solving upstream key automation.

## Dependencies at Risk

**opencode is pinned and tightly coupled to harness-side workarounds:**
- Risk: `OPENCODE_VERSION` is pinned to `1.15.7`
  (`agents/Dockerfile:103`); harness ships behavior workarounds keyed to
  this exact version (the headless stdout race, the `task` header parse, the
  json-stream empty-run fallback).
- Impact: An opencode upgrade can break the `-p` recovery path, the prompt
  paring, or model discovery. opencode self-update is disabled
  (`OPENCODE_DISABLE_AUTOUPDATE=1`, `architecture/containers.md:95`) so the
  pin holds, but staying pinned means missing upstream fixes.
- Migration plan: Bump deliberately, re-verify against the checklist in the
  `OPENCODE_VERSION` note in `agents/Dockerfile`, and run
  `full_pipeline_test` (T10) plus `TestTaskDescriptionParing` before
  shipping a bump. Treat any unverified bump as a known regression risk.

**Flask built-in server is not a production WSGI stack:**
- Risk: `app.run()` (`proxy/proxy.py:1866`) is the dev server.
- Impact: Single-flight request handling; one slow upstream call blocks
  concurrent requests for up to `PROXY_TIMEOUT` (180s default). No graceful
  worker model. Flask itself prints a "do not use in production" warning.
- Migration plan: Wrap with gunicorn or waitress (a few workers, matched to
  the single-key single-flight reality so workers don't all stall on the same
  locked key). Until then, document the serialization as a known limit.

## Test Coverage Gaps

Current red/yellow items are taken from `tests/COVERAGE.md` (as of this
audit: 243 green, 5 yellow, 118 red of 366 IDs — 66.3% green,
`tests/COVERAGE.md:60-65`). The former `O###` ollama section is gone (0 IDs,
`tests/COVERAGE.md:78`). The largest gaps below are the ones most likely to
hide a real regression in the post-migration topology.

**Upstream HTTP error handling is entirely untested (proxy):**
- What's not tested: P037-P042 (`tests/COVERAGE.md:309-314`) — upstream `401`
  unlock-URL forwarding, `403`, `429`, `5xx`, connection-failure→502, and
  non-JSON→502. All red.
- Files: `proxy/proxy.py` upstream-call + error path (the `requests.post`
  around `proxy/proxy.py:1690-1695` and its `_client_error` envelope).
- Risk: The key-lock flow is the single most operationally important error
  path (it fires every ~8h) and nothing asserts the proxy forwards the
  `401` + `unlock_url` shape, or that an unreachable upstream becomes a clean
  `502` rather than a 500/stacktrace.
- Priority: High — this is the live failure mode users hit most.

**Debug-dump file contents are unverified (proxy):**
- What's not tested: P043-P049 (`tests/COVERAGE.md:315-321`) — no test reads
  back any of the `01`/`02`/`03`/`04`/`99` dump files or the filename
  pattern. All red.
- Files: `proxy/proxy.py:346-355` (`save_debug_file`) and its call sites.
- Risk: A refactor could start dumping the `Authorization` header (currently
  excluded — see Security) or corrupt the dump format, and no test would
  catch it. Given the key-not-in-dumps invariant is a security property,
  this gap is more than cosmetic.
- Priority: Medium-High — add at least one assertion that the `02` dump has
  no `Authorization`/`Bearer` string.

**Agent entrypoint idempotency and launch flags (agent):**
- What's not tested: A005 "entrypoint run twice is a no-op for UID remap"
  (`tests/COVERAGE.md:343`) and the agent-launch flag matrix — F045
  (`harness shell`), F046 (`--yolo` pass-through), F047 (`--net`), F050
  (multiple `--mount`), F052 (`--print` long form)
  (`tests/COVERAGE.md:166-173`). All red. The A prefix is the worst-covered
  agent area (10/22 green, `tests/COVERAGE.md:73`).
- Files: `agents/entrypoint.sh`, agent-launch path in `harness`
  (`run_agent`/`cmd_shell`).
- Risk: The entrypoint runs firewall + UID remap + gosu drop on every launch;
  a re-run regression (e.g. re-seeding the home, re-chowning) would be silent.
  Launch-flag regressions would mis-wire the container.
- Priority: Medium — these are interactive/TTY-heavy paths that are hard to
  assert, which is why they are red; `full_pipeline_test` exercises the happy
  path end-to-end.

**Net subcommand surface (CLI):**
- What's not tested: F078-F092 (`tests/COVERAGE.md:205-219`) — `net deny`
  no-op, `net edit`, `net open` confirmation phrase and mismatch rejection,
  `net close` and its idempotency/JSON-key-drop, unknown-service handling,
  `harness unlock` against mocked/unreachable upstream. All red. N prefix is
  6/30 green (`tests/COVERAGE.md:75`).
- Files: `scripts/lib/net_helpers.sh`, `cmd_net_*` and `cmd_unlock` in
  `harness`.
- Risk: The firewall override flow is a security boundary (`net open` grants
  unrestricted egress); its confirmation-phrase gate and the override
  JSON-key cleanup are unasserted.
- Priority: Medium-High for the `net open`/`net close` override paths
  (security-relevant); lower for `net edit`/unlock (require `$EDITOR`/network
  mocking).

**jq-less fallback and upgrade `--rebuild` (CLI):**
- What's not tested: F016 (`tests/COVERAGE.md:134`) — host-jq-absent docker
  fallback never forced; F038 (`tests/COVERAGE.md:156`) — `harness upgrade
  --rebuild`; F032 (`tests/COVERAGE.md:150`) — `harness update` refusing a
  dirty tree. All red.
- Files: `harness_jq`/`_ensure_jq_sidecar` and `cmd_upgrade` in `harness`.
- Risk: The jq sidecar fallback (`architecture/harness-cli.md:260-293`) is a
  fragile lifecycle (sweep/reap of `harness-jq-$$` containers) used on every
  jq-less install; its docker branch is the one not exercised.
- Priority: Medium.

---

*Concerns audit: 2026-06-02*
