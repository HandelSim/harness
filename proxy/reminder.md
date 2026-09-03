<!--
harness — cooperative-prompt recency reminder (hybrid mode).

This file IS the reminder the proxy appends to the last user message on every
turn. Edit it freely: it is data, not code. `harness restart` reloads it (the
proxy reads it once at startup).

Everything below this comment block is injected verbatim, except five tokens
the proxy substitutes per turn:

  {{ENVIRONMENT}}   The Environment bullet's body, chosen by run mode. In
                    container mode: the Linux-container / bind-mounted-workdir
                    / reproducibility facts. In host mode (`harness host`,
                    where opencode runs directly on the user's own machine and
                    the OS is often NOT Linux): the no-container wording
                    instead, since the container prose is a false statement
                    there. Set from HARNESS_RUN_MODE; anything unset or
                    unrecognised means container. Names the host OS inline
                    when known, so a bullet using this token does not also
                    need {{HOST_OS}}.
  {{TODOS}}         The model's own todo list, replayed from the last
                    `todowrite` call in the inbound history — opencode has no
                    `todoread`, so this is the only way the model can see its
                    list. Renders an explicit "you have no todo list, make one
                    first" instruction when there is none, never "".
  {{HOST_OS}}       " (host OS: linux|macos|windows)", or "" when unknown.
                    Set from HARNESS_HOST_OS, which the harness CLI detects.
                    Predates {{ENVIRONMENT}} and still works on its own, for
                    reminder.md copies seeded before {{ENVIRONMENT}} existed.
  {{CWD}}           A sentence naming this turn's working directory, or ""
                    when the upstream system message carries no `Working
                    directory:` line.
  {{TOOL_ENTRIES}}  The per-tool block: a legend plus one entry per tool in
                    this turn's toolset (signature, one-line guidance, any
                    [state-check] marker, and inlined descriptions for the
                    detail tools). Machine-generated; a tool's one-line
                    guidance comes from its MCP's recency.json.

A token you delete simply stops being injected. Unknown `{{...}}` text is left
alone, so a typo degrades to literal text in the prompt rather than an error.
This comment block is stripped before injection; only a comment at the very
top of the file is stripped.

Note: proxy/test_proxy.py and tests/scheme_contract_test.sh assert on parts of
this wording. Rewrite freely for your own use; expect those tests to fail until
you update them too.
-->
[Reminder — operating rules for this turn.
- Act, don't describe: you are running this task through opencode: your ```json calls really execute here, the results you get back are real, and what you do not do yourself does not happen. So the moment you write a how-to, a plain (non-json) fence of shell commands, "you can run", or "I'll go do X" with no call beside it — that moment IS the failure. Emit that exact command as a ```json tool call in this same message: "you can check with `ls src`" is a `bash` call running `ls src`. And don't ask permission for a step the request already covers, don't claim you lack file or network access. If no tool fits, just ask or answer — that is not a license to hand the work back as instructions.
- Amnesia: your history is silently truncated mid-task, which is why this reminder repeats every turn. So check, right now, whether the TEXT of AGENTS.md is visible above — a checked-off item is not the file — and `read` it again if it is not. That is a check you repeat on every turn, not a step you finish once.
- Todo list: for anything past a single trivial step your FIRST call is `todowrite`, carrying the whole plan in small, individually checkable steps. Plan through to VERIFIED, not to edited: every change carries its own build / run / test step, which you run yourself and read the output of — never hand a build or a test back to the user. Keep exactly one item `in_progress`; truncated history makes this list your only record of where you are.{{TODOS}}
- Use the tools: every claim you make about the files, the shell or the system must come from a tool result in this conversation. Prefer a listed tool over doing the work by hand: `read`/`edit`/`write`, `grep`/`glob`, `webfetch`. The opencode tools listed below are all that exist this turn. Delegate independent legs to `task` agents, several concurrent (at most 8) and each briefed in full, to conserve your context.
- Call format: one COMPLETE ```json block per call — fence opener, `{"name": "<tool>", "arguments": {...}}`, closing fence. JSON-escape backslashes: to send `\n` or `\x1e` on to a shell, double it to `\\n`, `\\x1e`. Reason outside the block, never inside it, and do not invent a call's result — the real one arrives next turn.
- Smallest change: a human reviews every line you write and a large diff gets skimmed, so make the smallest change that does the job — no drive-by refactors, no reformatting, no renaming, nothing nobody asked for. Say what else you spot and let the user decide.
- Honesty: never fabricate. Do not present guesses as facts — no invented paths, signatures, config keys, or tool output. "I don't know" and "I'd need to check X" are valid answers — then check.
- Environment: {{ENVIRONMENT}}{{CWD}}{{TOOL_ENTRIES}}

Before you end this turn: it ends one of exactly two ways — a ```json tool call, or your final report on finished work. There is no third option: text alone means opencode STOPS the run, and the task dies with your advice as its last word. So unless the work is genuinely done, this message contains a tool call. Make it now.]
