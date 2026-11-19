# W3-A: review2 Set A, appearance + background engine

Status: P1-1 to P1-5 done, and P1-6 done apart from the two items listed under "Skipped". Every
finding was checked against the code first; all of them were real.
No git operations were run here; the parent commits.

## Checks at hand-over

- `tsc --noEmit -p tsconfig.json`: clean.
- `node scripts/run-tests.mjs`: 258/258 pass (12:16 UTC, with the other fixers' tests; 247/247 at 12:05; includes the 19 new tests: settings 5, appearance 1, runstate 2, restore 4, jank 3, surface 4).
- e2e (`scripts/e2e.mjs` under the S/e2e.lock flock), run 1 (12:10-12:15 UTC): `e2e/bg/{appearance,engine,shell}.spec.ts`
  + `e2e/views/shell.spec.ts`: 16 passed, 1 skipped (the screenshot test, which runs only with
  `HARNESS_DESKTOP_E2E_SHOTS=1`), 1 failed: the new OS-flip test. That was a race in the spec: the first
  dark read (0.7) was the old seeThrough-0.5 value, taken before the seeThrough 1 write had propagated.
  The light flip and the flip back re-resolved correctly (dark floor 0.665). The spec now waits for the
  settled floor. Run 2 (12:19-12:21 UTC, final tree): `e2e/bg/appearance.spec.ts` + `e2e/views/shell.spec.ts`: 8/8 passed.

## P1-1: accent presets (HIGH)

- `ACCENT_PRESETS` is a new export in `src/shared/ipc.ts`. This is an additive shared-contract change:
  `Settings.appearance.accent` stays `string`.
- `main/settings.ts` validates `appearance.accent` as an enum of those presets, with `iris` as the default.
  A value that is not a preset falls back to the current value, then to the default. An old
  `settings.json` with a hex accent such as `#7c9cff` therefore loads as `iris`, and the other fields are
  kept (`main/settings.test.ts`, 5 tests).
- `core/app.ts` writes `data-accent` only when the value is a preset and uses `iris` otherwise, so the
  `--accent-*` tokens can never go blank (`core/appearance.test.ts`).
- New e2e `e2e/bg/appearance.spec.ts` (a new file outside the Set A list; nobody else owns `e2e/bg/`): a swatch click sets `data-accent`, `--accent-9` resolves and
  `aria-checked` follows.

## P1-2: OS theme and reduced-motion changes reach the engine and the glass floor (HIGH)

- The new `appearanceTick` signal in `core/app.ts` is bumped (with `peek`, so no effect cycle) at the end of
  every `apply()`. That covers settings writes and the `prefers-color-scheme` / `prefers-reduced-motion`
  change listeners.
- `core/background.ts` keys three things on that tick: the engine theme / reduced-motion effect, the
  surface-alpha effect, and the picker thumbnails (`BgPicker.tsx`, effect deps `theme|accent`). Before this
  they keyed on `settings.value.appearance`, which did not change when only the OS changed.
- Engine `setTheme` drops the cached thumbnails and revokes their blob URLs after 30 s.
- Tests: `core/appearance.test.ts` stubs matchMedia and DOM. It checks that an OS flip rewrites
  `data-theme` / `data-reduce-motion` and bumps the tick. `e2e/bg/appearance.spec.ts` checks that an
  emulated OS flip under theme `system` changes `--panel-alpha-auto` and that flipping back restores it.

## P1-5: WebGL context loss that is never restored

- New `src/background/restore.ts` (`RestoreWatch`, pure, injected timers, 4 tests). On the first
  `contextLost` it arms a timer for `RESTORE_WAIT_MS` (4 s). A stats message with more frames than at the
  loss disarms it. If the timer fires while the layer is visible and running, GL is marked broken and the
  layer falls to the next rung through `apply(..., gpuBroken)`. While paused, the watch re-arms instead
  (a paused layer draws no frames, so there is nothing to judge). A second loss within 60 s keeps the
  existing "lost twice" path and clears the watch. A new layer and dispose clear it too.
- Also from P1-6: a failed `init` post (the worker is gone) now goes to the next rung instead of leaving
  a blank layer, `onmessageerror` counts as a worker failure, and `retire` deletes `lastInitGL` for the
  retired id (this used to leak).

## P1-4: jank relative to the display frame time

- `JankMeter` estimates the display frame time as the minimum per-window 10th-percentile interval (windows
  with n >= 20). It keeps the estimate across `reset()`. `jankThreshold(v) = max(34, 2v + 4)`. An estimate
  above 34.5 ms is ignored and the 60 Hz rule applies, so constant slow frames still count as jank. A
  30 Hz display (33 ms frames) is no longer reported as janky.
- Worker rung: `workerJank()` sends `p95Free` (p95 over the frames that do not overlap a long task) as p95,
  and `longMs: 0`. App long tasks are not the scene's fault and no longer step the worker governor down.
  The main2d rung still sees long tasks, because it shares the thread. Two new optional fields,
  `vsyncMs?` and `p95Free?`, were added to the `jank` ToWorker message (`protocol.ts`), validated as
  0..60000 when present.
- Reset on resize (`onResize` resets the meter). Reports whose window starts within
  `JANK_WARMUP_MS` (5 s) of engine creation are ignored, because that is app start-up.
- Tests: 3 new in `jank.test.ts` (threshold; 30 Hz is never janky and the estimate survives reset;
  workerJank drops long-task frames). The existing "slow frames show up" test still passes.

## P1-3: contrast floor

- `surface.ts` `worstPixels` now includes `rampMax` (the per-channel max of the ramp stops). Additive or
  overlapping light can reach that colour, but no single stop is that colour.
- Aurora (dark): the additive term is soft-kneed into `hi - c0` and clamped to `hi` (the ramp max), so a
  pixel never goes past the floor's worst case. The light theme is unchanged (it already darkened toward c0).
- Tracers: `source-over` in both themes (it was `lighter` in dark, which could pass every stop).
- Glass text set: the floor now covers `--ok/warn/err/info-text`, `--accent-11` and `--icon` as well as
  text-1/2/3 (`GLASS_TEXT_TOKENS`, exported from `core/background.ts`). The light tokens were darkened to
  at least 6:1 on surface-0, so the floor did not rise: light floors are still 0.649-0.754 and dark
  0.664-0.70.
  Light: icon `#5d6169`, ok `#1c6e4a`, warn `#8d5300`, err `#b8272c`, info `#0b63af`; accent-11 iris
  `#5652c4`, blue `#0b63af`, teal `#006e5f`, amber `#8d5300`, crimson `#b91a5a`. accent-9 is unchanged.
- `prefers-contrast: more`: the alpha override moved from `tokens.css` (where `base.css` overrode it, so
  it never applied) to `glass.css`, after `base.css`. In light it also gets stronger borders.
- Tests (in `surface.test.ts`, 9 in total): AA holds for every glass text over every palette and the
  ramp max (more than 2000 pairs checked); the default see-through floor is below 0.8; GLASS_TEXT_TOKENS
  matches; rampMax bounds every stop; source asserts on tracers, aurora and the prefers-contrast order.

## P1-6: lows

- Done: `onBattery` is wired. `power:state` (falling back to `appInfo.onBattery`) feeds the new pure
  `bgRunState()` (`views/background/runstate.ts`, 2 tests). With `static` (the default), the background
  pauses on battery power and the last frame stays.
- Done: the "Rotate" settings row is removed. No code implemented it. The key is still validated in
  `settings.ts`, so stored values load.
- Done: an "On battery" hint was added to SettingsView. The SettingsView swatches now use `ACCENT_PRESETS`
  (one list shared with the validator).
- Done: thumbnail blob URLs are revoked on a theme change, the lastInitGL leak is fixed, and
  onmessageerror is handled (see P1-5).

## Skipped / not done

- "Constructor double worker init": not a real issue today. `DEFAULT_BG` is `mesh`, a CSS layer, so the
  constructor never posts a worker init before the first `set()`.
- Remembering the governor level per id across preview hovers: the review marks it acceptable; not done.
- Feeding jank to the governor only on software GPUs: not done. Low-power `auto` already covers
  software rasterisers.

## Behaviour changes (for the parent / release notes)

- `background.onBattery` now acts on its value: the default `static` pauses the background on battery.
- The Settings "Rotate" row is gone.
- The light status, icon and link (accent-11) colours are darker.
- Tracers in the dark theme no longer glow additively.
- An old hex accent loads as `iris`.

## Known limitations (inferred, not measured)

- On a 60 Hz display where every frame of the scene takes 33 ms from the first window, the vsync
  estimate reads 30 Hz and the meter no longer calls that jank. The worker's own frame-cost rung still
  sees it.
- No node test drives the real `core/background.ts` effects (they need the bridge and the DOM). The
  wiring is covered by `appearance.test.ts` (the tick) and the e2e.
