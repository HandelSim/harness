# W1-D: queue edge cases and serve L14

Status: DONE (stopped 08:12 UTC, before the 08:50 box).

Heads-up for the parent: commit `3f05534` ("Nightly wiki-ingest sweep (personal)", 08:03 UTC)
picked up this leg's uncommitted work along with the other legs' (ticket_queue.py, serve/api.py,
serve/test_api.py, tests/queue_edge_test.py, the three test docs, architecture/harness-cli.md, plus
`harness`, tests/unit_config_test.sh and desktop/ files from other legs), all under a wiki commit
message. Only the QUEUE_WORKFLOWS.md wrapper-row edit and this file are still uncommitted. This leg
did not commit anything. Something outside the legs (the nightly wiki job) commits in this repo.

## Done
1. **SIGKILL in `make_clone`**: `CloneKillTest` (2 tests) kills a child with SIGKILL inside `git clone`
   (a cut `.git`) and again after `refs/harness-queue-ref/KEY` is made in the project. The next run
   clones again and reaches `done` with no `failed` entry. The ref is gone from the project and the
   clone, and the branch is pushed. This needed no code change because `make_clone` was already safe.
2. **401 everywhere**: the wait was already bounded (outage_wait, 6 h), so it is not endless. It was
   worded wrong, though ("does not answer"), and on the launch path a 401 counted as a `LaunchFailed`
   against the ticket. That was a bug, and the fix is minimal. `key_refused` (HTTP 401/403) gives the
   wait its own reason, KEY_REFUSED ("the upstream refuses the key ... harness unlock"), in
   `waiting.why` and in the exit-3 line. After a `LaunchFailed`, `refused_key` asks the upstream once
   and, on a 401/403, treats the failure as that wait, so it is not counted. The run still continues
   by itself once the key works. Covered by `KeyRefusedTest` (3 tests: a unit test, the verifier
   getting 401, and every launch dying while the key is refused).
3. **Clock jumps**: `ClockJumpTest` mocks the time for `beating` and also moves a live run's beat
   ±1 h. Neither direction shows a spurious "run gone" or a second runner (run.lock refuses it), and
   the next beat restores running. No queue code change was needed.
4. **Disk full mid-run**: the run used to exit 0. Now `run_tickets` re-raises ENOSPC/EDQUOT, `main`
   prints the full-disk line and returns 1, and the next run finishes the ticket
   (`StateDirTest.test_a_disk_that_fills_mid_run...`). An OSError on a path outside the state folder
   now names that path instead of blaming the state folder (new test).
5. **Project variants**: `ProjectVariantTest` (7 tests on real temp repos with
   `init.defaultBranch=main`): not a repo, no commits, detached HEAD, worktree, a repo inside another,
   a subfolder plus a symlink, and a folder gone after add. Spaces/unicode and symlinked folders were
   already in `ProjectShapeTest`.
6. **Leftover lows** from review1-core:
   - Q-3: without /proc, `trusted` falls back to `ps -o command=` (`NoProcTest`).
   - Q-4: SIGHUP now keeps a file/pipe/socket stdout, so runner.out keeps the end line. Before,
     stdout and stderr always went to /dev/null.
   - Q-5: a parallel parent kills a slot that is still putting back after 120 s.
   - Q-6: a second TERM/HUP 60 s after the first ends a hung put-back (`SIGNAL_AGAIN`, new
     SignalTest).
   - A-5: vault paths go through `vault_view.resolve` (backslash, `..`, `.git`).
   - A-6: a 500 names the exception type only.
   - A-7 (part): `_cached` serves the stale value while one thread refreshes it (`CacheTest`).
   - L14: the vault tree hides only `.git`, so `.gitignore` and `.github` are listed and readable
     (`VaultPathTest`).
7. **Docs**:
   - QUEUE_WORKFLOWS: the GAP rows that are now closed are filled in, and bugs 7-12 are added. The
     wrapper-TERM row is now marked fixed by 91a1a74.
   - INVENTORY and COVERAGE: Q009 is corrected and Q012-Q016 are added.
   - architecture/harness-cli.md: signals, disk full, key refused, the ps fallback, and serve's vault
     paths, cache and 500 text.

## Test results (real, this session, nice/ionice/timeout)
- `python3 tests/queue_edge_test.py`: Ran 37 tests, OK.
- `cd serve && python3 -m unittest -q test_api test_pub`: Ran 101 tests, OK (CacheTest and
  VaultPathTest also run alone: 3 OK).
- Affected queue_test.py classes (StallTest, OwnerControlTest, ParallelTest, ReviewFixesTest): Ran 80,
  OK.
- Negative checks: with the fixes reverted, the launch-401 and disk-full tests fail.
- `py_compile` passes on all four Python files. pyflakes and flake8 are not installed.
- Not run (per brief): tests/unit_queue_test.sh in full, and the full queue_test.py.

## Skipped / remaining
- Q-7 (narrow TERM windows: in `beating_run`'s `finally`, and between `Popen` and `kids.append`):
  not done. Closing it needs signal masking, which did not fit the time box.
- A-7: a JSON 501 for PUT/DELETE/OPTIONS and stopping the ticket_state rebuild on every poll are not
  done. Only the cache part landed.
- Q-5's 120 s slot bound is checked by reading the code only (a test would need a put-back hung for
  over 2 min).
- Q-3 on Windows: there is no ps fallback there, so the beat alone decides. Untested.
- KEY_REFUSED mentions "a new key with harness config set". I did not check the exact
  `config set` key name because `harness` belongs to another leg. The `config set` owner should
  confirm the wording.
- The wrapper TERM/HUP forwarding (in `harness`, 91a1a74) has no test that signals the wrapper alone.

## For other owners (not this leg's files)
- **serve/runs.py ~1452**: `now - out["beat"] < RUN_FRESH` has no lower bound, so a beat from the
  future (the clock jumped back) shows the queue as running until the clock catches up. The fix is
  `0 <= now - beat < RUN_FRESH`, the same as `ticket_queue.beating`.

## Contract changes
None. No API shape changed. The JSON `/vault/tree` now also lists dot-entries other than `.git`.
