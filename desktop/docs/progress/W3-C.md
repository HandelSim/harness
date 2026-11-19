# W3-C: harness-core review 2 fixes

Source: review2-core.md (scratchpad). Each finding was checked against the code before changing it.
No git operations (the parent commits). Docker-free checks only; tests/unit_queue_test.sh not run (parent runs it).

## Done

1. **Future beat counted as a live run** (finding 1, verified): `serve/runs.py` `runner()` and the parallel
   slot check now use `0 <= now - beat < RUN_FRESH`, the queue's own rule. Test
   `test_runs.py::test_a_beat_from_the_future_is_a_run_that_is_gone` (run.json and run-I.json beats an hour
   ahead: not running, no slot in hand; `resume_runner` starts `queue run --host`). Fails with either half reverted.
2. **pub.log grows without bound** (finding 2, verified): `serve/pub.py` Handler gets `log_request` that skips
   GET/HEAD 200/304 of `/api/v1/poll`, `/live`, `*/live`, `/healthz`; errors and every other request still
   logged. Test `test_pub.py::PubServerTest::test_polls_that_went_well_are_not_logged` (fails without the filter).
3. **.env mode after `config set`** (finding 5, CLI half, verified 644 before): `_config_write_key` creates the
   tmp file under `umask 077`, then gives it `.env`'s own mode (GNU `stat -c %a`, else BSD `stat -f %Lp`;
   neither: stays 600; `-L` so a symlinked .env gives its target's mode, not 777). Test T19 in
   `tests/unit_config_test.sh` (600 stays 600 under umask 022, argv and --stdin; 640 kept; symlinked .env
   stays 600; no tmp left). Fails without the fix ("644 after config set"; without `-L`: "became 777").
4. **Launch update check** (finding 3): `_update_check_and_banner` returns at once under
   `HARNESS_QUEUE_LAUNCH`; origin is asked at most once per `HARNESS_UPDATE_CHECK_TTL` (default 1800 s) using
   the stamp `state/.harness-update-asked`, touched before each lookup (offline pays the timeout once per
   window), cache answers inside the window, `age >= 0` so a future stamp does not pin it. mtime via
   `stat -c %Y` / `stat -f %m`. Test T8 in `tests/unit_update_check_test.sh` (T1-T7 run with TTL=0 so they
   keep asking origin). Fails on the pre-fix harness.
5. **pid trust** (finding 4): new `_pid_cmdline` (/proc, else `ps -o command=`).
   - `host_proxy_pid` trusts a pid only while an argument is `$clone_dir/proxy/proxy.py`
     (`_host_proxy_is_ours`); unreadable command line and Git Bash keep the old behaviour; otherwise the
     pid file is dropped. Test T6 in `tests/unit_host_proxy_lease_test.sh` (a plain sleep in proxy.pid: not
     running, pid file removed, `host_proxy_stop` does not signal it). Fails without the fix.
     The lease test's `fake_proxy` and unit_host_test T15's listener now carry the proxy.py path.
   - `serve_running`: the word before `serve` must be a path named harness that resolves (absolute, or
     relative via /proc/PID/cwd) to this script / `$clone_dir/harness`; unresolvable (relative on macOS)
     goes by the name. Test S14 in `tests/unit_serve_test.sh` (proxy.py, `harness queue run`, another
     clone's harness: no; this harness serve, `./harness serve` from the clone: yes). Fails with the old
     `*harness*` check.
7. **Settings secrets on argv** (finding 5, serve half): `settings_view.save_config` runs
   `harness config set KEY --stdin` with the value on stdin for every changed value (HTTPS_PROXY can carry
   a password and is not flagged secret); `run_harness` gained `input=`; its timeout message names only
   `args[:3]`. `test_pub.py` SettingsPageTest fake harness records the stdin value; assertions updated.

Docs: `architecture/harness-cli.md` (config write mode, Settings via --stdin, future beat, pub.log filter,
serve_running / host_proxy_pid trust, update-check window and queue skip).

## Skipped

6. **ticket_state memo** (finding 6): not done. `serve/api.py` already memoises `ticket_state` per poll
   (`_ticket_st`, on state.json/notes.json/stop stats), so the API gain is smaller than estimated. A shared
   memo in runs.py needs an audit of 24 callers for in-place changes to nested lists/dicts (or a deepcopy
   that costs about what it saves); not finishable and verifiable inside the time box.
   Finding 7 (TERM during the upgrade wait) was not in this leg's list.

## Checks (results in the headline returned to the parent)
All run at 12:10-12:18 UTC on the final tree:
- `bash -n harness` OK; `scripts/check_runtime_calls.sh` OK; shellcheck -S warning: no warnings in the
  changed harness functions; test-file warning in new S14 code fixed (SC2178), the rest are pre-existing.
- `python3 serve/test_runs.py` 110 OK; `serve/test_pub.py` 60 OK; `serve/test_api.py` 48 OK.
- `tests/unit_config_test.sh` CONFIG TEST PASSED (T19 new).
- `tests/unit_update_check_test.sh` UPDATE CHECK TEST PASSED (T8 new).
- `tests/unit_host_proxy_lease_test.sh` 6/6 (T6 new); `tests/unit_host_test.sh` HOST TEST PASSED.
- `tests/unit_serve_test.sh` SERVE TEST PASSED (S1-S14; S14 new).
- Not run (per brief): tests/unit_queue_test.sh, docker sections. harness_test.sh T23 (docker) was read
  against the new update-check window and should still hold (no stamp: asks origin; offline: cache).

## Files changed
serve/runs.py, serve/test_runs.py, serve/pub.py, serve/test_pub.py, serve/settings_view.py, harness,
tests/unit_config_test.sh, tests/unit_update_check_test.sh, tests/unit_host_proxy_lease_test.sh,
tests/unit_host_test.sh, tests/unit_serve_test.sh, architecture/harness-cli.md, this file.
No git operations. serve/api.py not touched.
