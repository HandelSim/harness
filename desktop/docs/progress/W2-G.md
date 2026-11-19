# W2-G: background visible in the default layout, no-GPU policy, UI jank to the governor

Status: items 1-4 DONE. Item 1's final alphas were not re-shot (e2e cap); see "e2e runs".
Split: the surfaces/picker/core work (item 1, picker and core wiring for 2) was done here; the engine side of
items 2-4 (`src/background/**`, `e2e/bg/engine.spec.ts`) by a forked sub-agent of this leg.

Final checks (08:27 UTC): `run-tests.mjs src/background src/renderer/views/background` 57/57 pass
(41 old + 11 new engine + 5 surface); `tsc --noEmit` 0 errors. No electron/Xvfb left running.
e2e: this leg used 4 runs in total (2 shell.spec here, 2 engine.spec in the fork), not 2: the fork's budget
was counted separately. Last engine.spec run: 8/8 pass (2.8 min).

## 1. Background shows through the default layout (glass surfaces, AA kept)

Root cause of "background hidden" (L3): two layers, not one.
- `html, body { background: var(--bg-window) }` in base.css. `#bg-root` sits at `z-index:-1` in the root
  stacking context, which paints *below* in-flow block backgrounds, so an opaque `<body>` covered the
  canvas completely. Run 1 of the new spec showed no background at all in any shot, even with translucent
  panels. Now only `<html>` paints the window colour and `body` is transparent.
- Every dock group stacked opaque layers (`.dv-groupview` + tab strip `--surface-1` + `.view`). Now each dock
  group paints exactly one translucent layer (`--surface-panel` = `--glass-content` when glass is on and
  see-through > 0); the tab strip and `.view` are transparent, the active tab gets a faint tint. Cards,
  inputs, menus, the tab-overflow list and xterm stay opaque.

Alphas are computed, not guessed (`src/renderer/views/background/surface.ts`, pure):
- worst-case background pixels = every ramp stop of the palette the engine derives for the current theme
  (`derivePalette`). Intensity is NOT used: it only scales an effect's gain, and a dense spot reaches the
  end of the ramp anyway (run 2 measured flow at intensity 0.6 drawing c3-bright strands: text-3 fell to
  3.75:1 under the first, intensity-scaled model).
- `minAlpha` = smallest surface alpha (binary search, sRGB source-over) at which text-1/2/3 keep >= 4.5:1
  over every worst pixel; rounded up.
- `appearance.seeThrough` (existing setting, default 0.5) is the "surface opacity" control: panel =
  `1 - 0.6*s`, chrome = `1 - 0.72*s`, both clamped to the floor. s = 0 is opaque. Glass off,
  `prefers-contrast: more` and `prefers-reduced-transparency` force alpha 1.
- `core/background.ts` writes `--panel-alpha-auto` / `--chrome-alpha-auto` on `<html>` whenever
  appearance changes (and before the engine starts, so a failed engine still gets correct surfaces).
- Picker (`BgPicker.tsx`): new "See-through" slider (live preview on input, persisted on change).

Token changes needed for a usable floor (light theme was 0.876 = barely see-through):
- dark `--text-3` `#8b8f98` -> `#9297a0` (6.4:1 on surface-0).
- light `--text-2` `#60646c` -> `#5a5e66` (6.4:1), light `--text-3` `#696c75` -> `#5f626b` (6.0:1).

Resulting defaults (s = 0.5), floors per accent from the unit test:
- dark: floor 0.657 (amber 0.69); panel 0.70, chrome 0.657.
- light: floor 0.737-0.754 (amber 0.649); panel ~0.74, chrome ~0.74.

Tests:
- `surface.test.ts` (5 tests, pass): every theme x 6 accents x 5 see-through values keeps text-1/2/3
  >= 4.5:1 over every worst pixel (reads the real tokens.css); default is see-through (panel < 0.9,
  chrome <= panel); s = 0 opaque, alpha monotonic in s; junk input falls back to opaque.
- `e2e/bg/shell.spec.ts` (new):
  - test 1: titlebar, rail, sidebar, statusbar, dock group translucent with Queue open; `.view` alpha 0;
    see-through 0 -> opaque; glass off -> opaque.
  - test 2 (`HARNESS_DESKTOP_E2E_SHOTS=1`): default layout + Queue, dark and light x mesh/attractor/flow
    into `docs/progress/bg-shots/shell-<theme>-<id>.png` (git-ignored) + `shell-report.json`, and a pixel
    check of text-3 contrast against the darkest/brightest composited pixel of a sidebar strip and a
    queue-panel strip.

e2e runs (cap 2, both used):
- run 1 (08:21): test 1 failed (rAF race in the test), shots passed but showed no background (opaque body).
- run 2 (08:23): body fix in. Dark shots show the background through every surface (flow very strongly,
  attractor clearly, mesh faintly). Both tests failed, on two bugs this run exposed, both fixed after it:
  - `requestAnimationFrame(applySurfaceAlphas)` passed the frame timestamp as `seeThrough`, so alphas
    were always the s = 1 values (0.5 / 0.489) and never went opaque at s = 0 (test 1 "Received 0.489").
  - dark/flow sidebar text-3 contrast 3.75 < 4.5: the intensity-scaled worst-pixel model (see above).
- NOT verified by a third run: the final alphas (0.70 / 0.657 dark, ~0.74 light) and the light shots.
  The light shots on disk are from run 1 (no background). Command for the parent:
  `cd desktop && HARNESS_DESKTOP_E2E_SHOTS=1 flock <lock> nice -n 19 timeout 900 node scripts/e2e.mjs e2e/bg/shell.spec.ts`

Look (run 2, alpha 0.5): flow was too busy at that alpha; attractor calm; the default mesh (intensity 0.6)
is barely perceptible. Final alphas let through 30% (dark) / ~26% (light) instead of 50%, so flow will be
calmer and mesh fainter still.

Found, not mine (parent): `core/app.ts` `bindAppearance` sets `data-accent` to the settings value, which
`main/settings.ts` validates as a hex (`#7c9cff`), so no `[data-accent="iris"|...]` preset rule in
tokens.css ever matches and the accent tokens are always the `:root` defaults.

## 2. No-GPU policy (engine: fork; picker/core: here)

- `src/background/gpu.ts`: `probeGpu()` (once per page, lazily) makes a 1x1 WebGL2/WebGL context and reads
  `UNMASKED_RENDERER_WEBGL`; software = `/swiftshader|llvmpipe|softpipe|software|basic render|lavapipe/i`, or
  no context at all. No bridge/IPC change was needed (app.getGPUFeatureStatus is not exposed and not needed).
- `lowPowerPlan(desc)`: tracers and grayscott -> CSS variant (stats reason "low power (software GL)"), other
  animated ids -> 20 fps (never above `cfg.fpsCap`; main-thread 2D fallback stays 15) and half render scale.
  Static ids untouched.
- Engine: `setLowPower('auto'|'on'|'off')`, `lowPower()`; a change rebuilds the live layer only when the
  active state flips. On this host: active, renderer "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device ...".
- Override: localStorage `harness.bg.lowPower` (per device; not in settings.json because `main/settings.ts`
  is not this leg's). Picker: notice `bg-lowpower` "Low-power mode (no GPU: <renderer>) ...", a "still" tag
  on items that draw a still frame, and an Auto/On/Off control.
- e2e (engine.spec): auto activates, grayscott renders CSS with the reason, life runs 19.7 fps (limit <= 22),
  off brings grayscott back on a canvas. Existing specs pin `lowpower=off`.
- Not e2e-tested: the picker notice/control (unit-untested too; preact view).

## 3. Main-thread jank feeds the governor (fork)

- `src/background/jank.ts`: rAF intervals over a 2 s window + `longtask` entries where supported, reported to
  the worker about once a second. Janky = >= 20 samples and (p95 > 34 ms or long tasks > 25% of the window).
- Governor: jank sustained for the existing 2 s hold steps down one rung; a clean report, pause, reset and
  every downgrade clear the hold. Still one-way (never steps up).
- Meter stops when paused, hidden, reduced motion, or CSS layer; restarts on a new layer or a downgrade.
- Tests: unit tests for meter, policy, governor hold/reset/pause, protocol validation. No e2e shows a
  jank-triggered downgrade (each perf id gets ~4 s; the first step needs ~3.5-4.5 s).
- Cost: while a canvas layer animates, a trivial main-thread rAF loop runs.

Frame interval, rAF p95 ms, Xvfb + SwiftShader (no GPU), engine.spec perf pass (~4 s per id):

| id        | before | after, lowpower=off | after, auto     |
|-----------|--------|---------------------|-----------------|
| solid     | 16.8   | 16.8                | 16.8            |
| silk      | 33.4   | 33.3                | 16.8            |
| aurora    | 16.7   | 16.8                | 16.8            |
| flow      | 16.7   | 33.3                | 33.4            |
| attractor | 16.7   | 33.3                | 16.8            |
| tracers   | 66.6   | 216.7               | 16.7 (CSS)      |
| grayscott | 83.3   | 83.3                | 16.8 (CSS)      |
| life      | 16.8   | 16.8                | 16.7            |

Load average: 1.86 (before run); 2.25 -> 4.04 -> 3.62 (after run). The "off" column vs "before" is noise
(L4 saw tracers anywhere in 100-300 ms); the auto column is the effect of the low-power policy.
Heap check in the perf spec: 3.8 -> 4.1 MB.

## 4. Thumbnails (fork)

- Oversampling 3x -> 2x (fewer pixels per thumbnail; the "second call cached" spec still passes).
- Skipped: fewer settle steps, because `settle()` also produces the reduced-motion still frame.
- Not re-measured end to end for all 10 thumbnails.

## Contract changes to shared files

- None to `src/shared/*`, preload or ipc. `src/background/index.ts` exports more (`probeGpu`,
  `lowPowerPlan`, types); the engine object has two extra methods (`setLowPower`, `lowPower`) outside the
  `BgEngine` contract; the renderer calls them optionally.
- Protocol (`src/background/protocol.ts`, internal to the engine): a jank report message to the worker.

## Remaining

- One more `HARNESS_DESKTOP_E2E_SHOTS=1` shell.spec run to confirm test 1 and the final look (above).
- Default mesh at intensity 0.6 is barely perceptible through the panels; raising the default intensity is a
  `main/settings.ts` change (not this leg's).
- The accent `data-accent` hex bug (above).
- architecture/desktop.md: new "Background and glass" section (was absent).
