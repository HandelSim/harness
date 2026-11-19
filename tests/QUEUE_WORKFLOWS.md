# Ticket queue: workflows and edge cases

What `harness queue` (`cmd_queue` → `scripts/ticket_queue.py`, state in
`state/queue/`) does in each workflow and edge case, what the code does today
(read from `scripts/ticket_queue.py`, or seen in a probe run where marked
*probe*), and the test that pins it. **GAP** means no test, with the reason.

Tests live in `tests/queue_test.py` (`QT`, 302 tests) and
`tests/queue_edge_test.py` (`QE`, the cases this map found uncovered). Both are
docker-free; `tests/unit_queue_test.sh` runs both (Q1, Q1b). A name is
`Class.test_name` in that file.

State files referred to: `KEY/state.json` (the ticket), `KEY/stop` (stop
sentinel), `halt` (halt sentinel), `queue.list` / `archived.list` (order),
`run.lock` (flock held by the run), `run.json` (the run's beat every 3 s;
a beat over 30 s old, or one from the future, means no run), `run-I.json`
(a parallel slot's beat), `queue.lock` (owner commands and take), `*.tmp`
(write_atomic's temporary file, renamed into place after an fsync).

## Bugs fixed

| # | Bug (before) | Fix | Test (failed before, passes after) |
|---|--------------|-----|------------------------------------|
| 1 | SIGTERM (a shutdown, serve's `end_process`, `kill`) or SIGHUP (terminal closed) killed the run at once. The agent, in its own session, worked on with no one watching it (in place: in the owner's folder); its container was not stopped; the ticket stayed `running`; a parallel parent's slots never got the signal. | `Terminated(KeyboardInterrupt)` raised by handlers `cmd_run` installs (`Terminated` ~234, `catch_term` ~246, `cmd_run` ~5761 in scripts/ticket_queue.py). The Ctrl-C path runs: agent and container stopped, ticket `queued` at its stage with an `interrupted` entry; exit 128+N. `run_parallel` forwards the signal to each slot. `run.json` is kept with its beat already stale (`beating_run` ~5824) so serve's `resume_runner` starts the run again, as after a power cut. | `QE SignalTest.test_sigterm_stops_the_agent_and_puts_the_ticket_back`, `test_sighup_stops_the_agent_and_puts_the_ticket_back`, `test_sigterm_to_a_parallel_run_reaches_its_slots` |
| 2 | `halt --force` SIGKILLed whatever pid a stale `run.json` / `run-I.json` named. In the seconds between a new run taking `run.lock` and its first beat, run.json is still the one a run the machine took down left; after a reboot that pid can be any process. | `end_run` (~7212) kills a pid only when `trusted` (~7372): its file beats, or `/proc/PID/cmdline` shows `ticket_queue.py` (a run frozen whole, its beat thread too, can still be ended). Else it waits up to 10 s for a beat, then refuses (exit 1, "has not beaten in 30 s"). Each `run-I.json` passes the same check. | `QE HaltForceTest.test_halt_force_spares_a_pid_a_stale_run_json_names` (and `test_halt_force_ends_a_frozen_run_that_no_longer_beats`, which keeps the old behaviour for a real frozen run) |
| 3 | A `state.json` that is valid JSON but not an object (`[1]`, `"x"`: a hand edit) crashed `list` (`AttributeError` in `print_list`) and the run's `pending()` on every look. | `Queue.load` (~354) returns None for a non-dict, as for unparsable JSON. | `QE UnreadableStateTest.test_a_state_file_that_is_not_a_ticket_is_skipped_not_a_crash` |
| 4 | A full disk or an unwritable state folder (ENOSPC/EACCES/EROFS...) gave a Python traceback from any command (`add`, `note`, `move`, ...). | `main` (~7863) catches those errnos only: one `[queue] could not write the queue's files ... the disk may be full, or DIR not writable` line, exit 1. Other OSErrors still raise. | `QE StateDirTest.test_a_full_disk_is_said_not_a_traceback`, `test_an_unwritable_state_folder_is_a_clear_error` |
| 5 | On a read-only state folder with no `run.lock` yet, `list`/`status` printed the list then failed (`run_going` opened `run.lock` for append). | `run_going` (~435): a lock file that cannot be made means no run holds one; an existing one is opened for reading. | `QE StateDirTest.test_an_unwritable_state_folder_is_a_clear_error` |
| 6 | Two more signal cases, found by the tests for bug 1. (a) SIGHUP from a closed terminal: putting the ticket back writes to the dead tty, raises EIO and exits 120, so the ticket's log lost its last line. (b) A run under `nohup` (SIGHUP ignored) would have been ended by SIGHUP once bug 1's handler replaced SIG_IGN. | `catch_term` (~246): on SIGHUP, stdout/stderr go to /dev/null before `Terminated` is raised; a signal already set to SIG_IGN keeps it. | `QE SignalTest.test_a_closed_terminal_still_puts_the_ticket_back` (exit 120 before), `test_a_nohup_run_is_not_ended_by_sighup` |

## Adding tickets

| Case | Expected | Actual (code) | Test |
|------|----------|---------------|------|
| Manual `add KEY --project P` | Queued at the bottom, base = the checked-out branch, in place, plan waits for approval | `cmd_add`; project resolved with `git rev-parse --show-toplevel` | `QT QueueTest.test_add_list_remove`, `StartAndBranchTest.test_defaults_are_in_place_and_wait_for_the_plan` |
| Jira row's Add (serve) | Same as manual add; the ticket text comes from the vault | serve builds a `harness queue add` command; the engine only sees the key and the vault's ticket note. Jira fetching is not in ticket_queue.py | Engine side: `QT AddGuardTest.test_empty_ticket_or_missing_project_blocks`. The serve dialog belongs to the serve leg |
| Duplicate key (any case) | Refused, existing ticket untouched | "KEY is already in the queue" (keys are upper-cased) | `QT QueueTest.test_add_list_remove` (line ~1319) |
| Same key through a symlink to the same project | Refused, project stored as the real path | realpath via `--show-toplevel` | `QE ProjectShapeTest.test_a_symlinked_project_folder_is_the_folder_it_names` |
| A branch already named after the ticket | Carried on (only forward); a branch another ticket uses needs `--allow-branch` | `start_and_base`, `ends_on` | `QT StartAndBranchTest.test_carry_on_an_existing_branch`, `test_onto_an_existing_branch_only_forward`, `AddGuardTest.test_branch_that_is_shared_needs_allow_branch`, `test_one_branch_for_many_tickets_is_refused` |
| `--start REF`, `--start here`, remote branch | Work starts there, ends on the ticket's branch | `start_of`, `commit_of` | `QT StartAndBranchTest.test_from_a_remote_branch_onto_a_new_branch`, `test_start_here_in_a_clone_is_the_commit_checked_out` |
| Empty ticket text / missing project | Blocks with why, no traceback | `process` checks | `QT AddGuardTest.test_empty_ticket_or_missing_project_blocks` |
| Owner command while `queue.lock` is held too long | `Busy`: "try again", exit 1 | `main` catches `Busy` | `QT AddGuardTest.test_owner_command_on_a_held_lock_fails_cleanly` |
| `edit` before / after the work starts | Before: anything add set; after: only stages to come | `cmd_edit` | `QT QueueTest.test_edit_changes_what_add_set_until_the_work_starts`, `test_edit_after_the_work_starts_changes_only_the_stages_to_come` |

## Ordering, start, resume

| Case | Expected | Actual | Test |
|------|----------|--------|------|
| `move up/down/top/bottom`, `--before` | Order in `queue.list` changes, by key | `cmd_move` under `queue.lock` | `QT OwnerControlTest.test_move`, `test_move_before` |
| Run takes tickets top to bottom, skips what waits on the owner | `pending()` | `run_tickets` | `QT QueueTest.test_whole_loop_pushes_the_branch` and most run tests |
| Two `harness queue run` at once | Second refused, first untouched | `run.lock` flock; "another 'harness queue run' is running", exit 1 | `QT ReviewFixesTest.test_run_lock_is_held_by_one_process` (lock only), `QE ConcurrencyTest.test_a_second_run_is_refused_while_one_goes` (two real runs) |
| `QUEUE_PARALLEL=N` / `--parallel N` | N slots, one ticket per project folder at a time | `run_parallel`, `elsewhere` | `QT ParallelTest.*` (4 tests) |
| `QUEUE_WATCH=1` (default) | An empty queue is watched until halt / Ctrl-C / upgrade | `--watch-queue 1` | `QT WatchTest.test_an_empty_queue_is_watched_until_a_halt`, `test_parallel_slots_each_watch_and_a_halt_ends_them_all` |
| `run --once` / `QUEUE_WATCH=0` | Ends when nothing is left | | `QT` run_queue helper (every run test) |
| Answered during the run | Planned again in the same run | | `QT QueueTest.test_answered_during_the_run_is_planned_again_in_the_same_run` |

## Stages

| Case | Expected | Actual | Test |
|------|----------|--------|------|
| plan → implement → review → fix → push | Each stage once, review again after fix, push to the remote | `process` | `QT QueueTest.test_whole_loop_pushes_the_branch` |
| Plan panel (N planners, merge) | Each planner, then a merge | `run_panel` | `QT PanelTest.*` |
| Blocking question / take defaults | Waits for answer / goes on with defaults | | `QT QueueTest.test_blocking_question_waits_for_an_answer`, `test_take_defaults_goes_on_without_answers` |
| No commit / a failed check | Go on once in the session, a new agent, then blocked | | `QT QueueTest.test_no_commit_is_retried_once_then_blocked`, `GoOnOnceTest.*` |
| test stage | Baselines, no framework blocks with how to go on | | `QT TestStageTest.*` |
| `.github/` change, made-up quote, unparsable file | Fails the change | | `QT QueueTest.test_ci_change_is_refused`, `test_a_made_up_quote_fails_the_change`, `test_a_changed_file_that_no_longer_parses_fails_the_change` |
| Push refused / no remote / `QUEUE_PUSH=0` | Blocked at push with the error / kept local | | `QT ResultsTest.test_a_refused_push_blocks_with_the_error`, `test_with_push_off_or_no_remote_the_branch_is_kept_local` |
| Push to a second remote (`--remote upstream`) | That remote, not origin | `remote_url(project, remote)` | `QE ProjectShapeTest.test_a_second_remote_is_pushed_to_when_named` |
| Follow-up round, owner review comments (`fix`) | Back through fix/review, then push | | `QT QueueTest.test_a_done_ticket_goes_round_again_with_a_follow_up`, `OwnerReviewTest.*` |

## Owner controls during a run

| Case | Expected | Actual | Test |
|------|----------|--------|------|
| `halt` | Agent killed, ticket back at its stage uncounted, run exits 0 | `checkpoint` raises `Interrupted("halt")` | `QT OwnerControlTest.test_halt_puts_the_ticket_back_and_ends_the_run`, `ParallelTest.test_halt_and_halt_force_end_every_slot` |
| A halt file left from before the run | Dropped; one asked as the run starts is kept | `new_run` compares with the lock time | `QT ReviewerFixesTest.test_a_halt_asked_as_the_run_starts_is_kept` |
| `halt --force` (end) on a run that does not halt | Run and agent killed, tickets put back | `end_run` | `QT OwnerControlTest.test_halt_force_ends_a_run_that_does_not_halt` |
| `halt --force` while run.json is stale (new run not beating yet, pid reused) | Refuses, kills nothing | **fixed** (bug 2) | `QE HaltForceTest.test_halt_force_spares_a_pid_a_stale_run_json_names` |
| `halt --force` on a run frozen whole (no beat, but its pid is a `ticket_queue.py` process) | Killed at once, no 10 s wait | `trusted` reads `/proc/PID/cmdline` (Linux; elsewhere the beat alone decides) | `QE HaltForceTest.test_halt_force_ends_a_frozen_run_that_no_longer_beats` |
| `stop KEY` (running / waiting / left by a dead run) | Sentinel for the run / stopped at once | `run_holds` decides | `QT OwnerControlTest.test_stop_the_running_ticket_and_the_run_goes_on`, `test_stop_a_waiting_ticket_and_resume_it`, `test_stop_a_running_ticket_with_no_run_left` |
| `retry --stage S`, `continue` | From that stage / in its session | | `QT OwnerControlTest.test_retry_from_a_stage`, `RetryMatrixTest.test_matrix`, `OwnerControlTest.test_continue_a_stopped_stage_goes_on_in_its_session` |
| `note` (inbox) during a run | Reaches every later stage; never overwrites a running ticket's state | `notes.json` separate from state.json | `QT OwnerControlTest.test_owner_notes_reach_every_stage`, `ReviewerFixesTest.test_a_note_does_not_write_over_a_running_ticket` |
| `remove` / `archive` a running ticket | Refused while in hand; allowed when left by a dead run | | `QT ReviewerFixesTest.test_a_running_ticket_is_not_removed`, `test_a_running_ticket_left_by_a_dead_run_can_be_removed`, `OwnerControlTest.test_archive_refuses_a_running_ticket` |
| Debug agent on a block | One look per stage, fixed → retried once | `debug_block` | `QT QueueTest.test_a_block_gets_one_debug_agent_and_fixed_sends_the_stage_again` and 6 more debug tests |
| Findings interplay | `findings` lists each ticket's findings note; the findings agent is a separate process (`scripts/findings_agent.py`) that adds tickets via `add` | `cmd_findings` reads only | Findings agent: its own tests (not this leg). `cmd_findings` output: GAP (read-only print, low risk) |

## Kills, crashes, hangs

| Case | Expected | Actual | Test |
|------|----------|--------|------|
| Ctrl-C (SIGINT) mid-stage | Agent and container stopped, ticket back, exit 130, run.json removed | `run_tickets` `except KeyboardInterrupt` | `QT OwnerControlTest.test_ctrl_c_stops_the_agents_container_too` |
| SIGTERM / SIGHUP mid-stage | As Ctrl-C, exit 128+N, run.json kept (stale) for serve's resume | **fixed** (bug 1) | `QE SignalTest.test_sigterm_stops_the_agent_and_puts_the_ticket_back`, `test_sighup_stops_the_agent_and_puts_the_ticket_back` |
| SIGTERM to a parallel parent | Each slot puts its ticket back | **fixed** (bug 1) | `QE SignalTest.test_sigterm_to_a_parallel_run_reaches_its_slots` |
| SIGTERM while watching an empty queue (`--watch-queue`) | Run ends 128+N; no ticket to put back; run.json kept stale (with `watch`) for serve's resume | outer handler / `cmd_run` | `QE SignalTest.test_sigterm_while_watching_an_empty_queue` |
| SIGHUP when the terminal closed (the tty is gone) | As SIGTERM; nothing more is written to the dead tty | **fixed** (bug 6a) | `QE SignalTest.test_a_closed_terminal_still_puts_the_ticket_back` |
| SIGHUP to a run under `nohup` | Ignored, the run goes on; SIGTERM still ends it | **fixed** (bug 6b) | `QE SignalTest.test_a_nohup_run_is_not_ended_by_sighup` |
| SIGTERM to `harness queue run` (the bash wrapper) alone | Python ends too | The wrapper runs python as a child (not `exec`), so python keeps going and the wrapper's `host_proxy_done` cleanup is skipped. serve's `end_process(tree=True)` signals the whole tree, so serve's Stop is fine. | GAP: `harness` is outside this leg; not tested |
| SIGKILL mid-stage | Ticket stays `running` with its agent on record; next run ends the orphan and goes on | `take` → `orphan` → `reap` (`is_agent` checks the launch id in /proc) | `QE SignalTest.test_sigkill_leaves_what_the_next_run_ends`, `QT OrphanAgentTest.test_run_ends_the_orphan_before_taking_the_ticket` |
| Killed at a stage boundary (during a second planner, while verifying) | Goes on from where it was without a new launch | stage records | `QT PanelTest.test_killed_during_the_second_planner_goes_on_with_it`, `StallTest.test_run_killed_while_verifying_resumes_without_a_new_launch` |
| Killed mid-clone | Clone made again (complete only once `base_sha` is saved) | `make_clone` | GAP: no test kills inside `git clone`; code path is `make_clone`'s pop/save of `base_sha` |
| opencode exits at once / keeps failing to launch | Not an attempt; then blocked | `LaunchFailed` | `QT StallTest.test_launch_that_dies_at_once_is_not_an_attempt`, `test_launch_failing_again_and_again_blocks` |
| Agent hangs (done but not exiting, idle, past the timeout) | Stopped and checked / retried / blocked | `idle-min`, `stage-timeout` | `QT StallTest.test_agent_done_but_not_exiting_is_stopped_and_checked`, `test_idle_agent_with_the_upstream_down_goes_back`, the 3 timeout tests |
| Proxy / upstream down | Ticket back, outage waited out (or exit 3) | `Infra`, `wait_out` | `QT StallTest.test_outage_is_waited_out`, `test_outage_past_the_wait_ends_the_run`, `QueueTest.test_upstream_down_stops_the_run_and_keeps_the_ticket` |
| Verifier hangs / answers garbage | Blocked, retry rechecks | | `QT StallTest.test_verifier_that_hangs_blocks_then_retry_rechecks`, `test_verifier_that_gives_garbage_blocks_then_retry_rechecks` |
| Upstream key expired (401 on every call) | Treated as an outage: the probe fails, the ticket goes back and waits | `ask_verifier` → `probe` → `Infra`; the agent's launch fails → `LaunchFailed` | Covered as an outage by the tests above; GAP: no test feeds a 401 specifically (from reading: same code path as a 502) |
| Vault save hangs | Later saves skipped | | `QT StallTest.test_a_vault_save_that_hangs_skips_the_runs_next_saves` |
| `harness upgrade` during a run | Exit 75 between tickets; `harness queue run` execs the new code | `code_sig` | `QT QueueTest.test_an_upgrade_restarts_the_run_between_tickets` |

## Restart, stale files, clocks, disk

| Case | Expected | Actual | Test |
|------|----------|--------|------|
| Reboot: stale run.json and run-I.json naming a reused pid, ticket `running` with an agent pid now someone else's, cut `*.tmp` files | Nothing reports a run going; the next run takes the ticket, kills no stranger, drops run-I.json, finishes | `beating`, `new_run`, `is_agent` | `QE RestartTest.test_the_run_after_a_restart_gets_past_what_the_dead_run_left`, `QT OrphanAgentTest.test_a_pid_that_is_not_that_launch_is_left_alone` |
| Lock files after a reboot | No stale lock: flocks die with the process | `try_lock` (flock) | `QT ReviewFixesTest.test_run_lock_is_held_by_one_process` |
| git `index.lock` older than boot | Taken away | `left_by_restart` | `QT RestartLeftLockTest.test_index_lock_older_than_the_boot_is_taken_away` |
| Half-written state.json | Cannot happen through `write_atomic` (fsync + rename); a cut or hand-broken one is skipped, `remove` still works | `Queue.load` returns None | `QT QueueTest.test_write_atomic_leaves_no_temporary_file`, `QE UnreadableStateTest.test_a_state_file_that_is_not_a_ticket_is_skipped_not_a_crash` |
| state.json valid JSON but not a ticket | Skipped | **fixed** (bug 3) | `QE UnreadableStateTest.test_a_state_file_that_is_not_a_ticket_is_skipped_not_a_crash` |
| Ticket in queue.list with no folder | `list` shows `? ?`; status/stats/remove work | *probe* | GAP (probe only; harmless) |
| Cut state.json: `retry`/`stop`/`note` | Say "is not in the queue" (misleading but harmless) | *probe* | GAP: message could name the unreadable file |
| `*.tmp` left by SIGKILL | Harmless; nothing reads them | nothing cleans them either | `QE RestartTest...` (proves harmless). GAP: never cleaned |
| Leftover `refs/harness-queue-ref/KEY` in the project after a kill mid-clone | The next clone of that ticket overwrites and deletes it | `make_clone` | GAP: no test (harmless ref) |
| Clock set back (beat from the future) | Not a run going | `beating` requires `0 <= now - beat < 30` | `QT QueueTest.test_a_beat_from_the_future_is_no_run` |
| Clock jumps forward | A fresh beat looks stale for one beat at most (re-beat every 3 s) | from reading | GAP: not tested |
| Disk full / state folder not writable | One clear line, exit 1; `list`/`status` still read | **fixed** (bugs 4, 5) | `QE StateDirTest.test_a_full_disk_is_said_not_a_traceback`, `test_an_unwritable_state_folder_is_a_clear_error` |
| Disk full in the middle of a run | The ticket keeps its last whole write, and the next run picks it up | `write_atomic` cleans its tmp; `run_tickets`' `except Exception` prints the error and the run goes on (exit 0); its next write to the ticket fails too, so the ticket stays `running` with `run.lock` freed and run.json removed; the next run takes it as an orphan and finishes it | `QE StateDirTest.test_a_disk_that_fills_mid_run_leaves_the_ticket_for_the_next_run` |

## Project shapes

| Case | Expected | Actual | Test |
|------|----------|--------|------|
| Not a git repo | Refused at add: "is not in a git repository (use --dir)" | *probe* | GAP (probe only) |
| Repo with no commits | Refused: "main is not a commit" | *probe* | GAP (probe only) |
| No remote | Kept local | | `QT ResultsTest.test_with_push_off_or_no_remote_the_branch_is_kept_local` |
| Multiple remotes | `--remote NAME` pushes there | | `QE ProjectShapeTest.test_a_second_remote_is_pushed_to_when_named` |
| Detached HEAD | Base and start are the sha; an agent commit on a detached head is taken | *probe* for add | `QT GitStatesTest.test_a_detached_head_with_the_agents_commit_is_taken`, `NestedReposTest.test_a_repo_branch_from_a_detached_head_is_put_back_there` |
| Dirty tree (in place) | Owner's files kept out of the ticket's commits | exclude block | `QT StartAndBranchTest.test_in_place_keeps_the_owners_files_out`, `test_in_place_commit_of_an_owners_file_fails` |
| Bisect / revert in progress | Blocks | | `QT GitStatesTest.test_a_bisect_or_revert_in_progress_blocks` |
| Submodules | The project's own; pushed first | | `QT SubmoduleWorkTest.*` (12 tests) |
| Linked worktree | Works in place | | `QT StartAndBranchTest.test_in_place_in_a_linked_worktree` |
| Nested repos / monorepo subfolder | Subfolder resolves to the top level; repos inside handled | *probe* for the subfolder | `QT NestedReposTest.*` |
| Symlinked project folder | Stored as the real path | | `QE ProjectShapeTest.test_a_symlinked_project_folder_is_the_folder_it_names` |
| Spaces and non-ASCII in the path; default branch `trunk` | Whole loop works, base `trunk` | | `QE ProjectShapeTest.test_a_trunk_branch_and_a_path_with_spaces_and_unicode`, `QT GitStatesTest.test_non_ascii_names_meet_the_file_gates` |
| Base branch deleted after add | Blocks with a reason naming it | | `QE ProjectShapeTest.test_a_base_branch_gone_since_the_add_blocks_with_why` |
| Branch checked out elsewhere | Not moved | | `QT ReviewerFixesTest.test_a_checked_out_branch_is_not_moved` |
| Large repo | Clone timeout 600 s; diff cut but every file kept in view | | `QT NestedReposTest.test_a_cut_diff_keeps_every_file_in_view`. GAP: no large-repo timing test |
| Windows paths / CRLF / long prompts | Clone options, task file, in-place needs host | | `QT AuditFixesTest.test_windows_clone_options`, `test_agent_paths_on_windows`, `test_a_long_prompt_on_windows_goes_through_the_task_file`, `StallTest.test_windows_stop_ends_the_agents_job` (simulated; no Windows CI here) |

## Remaining gaps (not fixed)

- `retry`/`stop`/`note` on a ticket whose state.json is cut say "is not in the queue" instead of naming the broken file.
- `*.tmp` files a SIGKILL leaves are never cleaned (harmless; nothing reads them).
- No test for: a kill inside `git clone`, a 401-specific upstream, a forward clock jump, or a large repo's timings.
- `harness queue run` (bash) runs python as a child, not `exec`: SIGTERM to the wrapper's pid alone leaves python running and skips `host_proxy_done`. A one-line fix (`exec`, or a `trap` that forwards the signal) belongs in `harness`, which this leg does not own.
- A disk that fills mid-run ends the ticket's stage with the error printed but exits 0 (the run goes on), so a scripted caller cannot see it from the exit code.
- serve's `RunsView.runner()` does not check `run.lock`, so a run killed with SIGKILL still shows as running for up to 30 s (by design, beat-based). After a SIGTERM the queue now marks its beat stale itself.
