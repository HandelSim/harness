# W4-CLI progress

- 16:48 started: design read (electron-builder.yml, NSIS per-user dir, linux exe harness-desktop, out dirs linux[-arch]-unpacked / mac[-arch]).
- 16:48 cmd_desktop + help + dispatch added to harness; tests/unit_desktop_cmd_test.sh passes (T1-T10), 5 mutations all caught.
- 16:50 done. bash -n harness OK; scripts/check_runtime_calls.sh OK; shellcheck clean on the desktop block and the new test;
  tests/unit_desktop_cmd_test.sh PASSED (T1-T10; 5 mutations of the code each caught); tests/unit_config_test.sh PASSED.
  Files: harness (cmd_desktop + _desktop_* helpers before "# --- dispatch", dispatch line, cmd_help entry),
  tests/unit_desktop_cmd_test.sh (new), architecture/harness-cli.md (table row + "`desktop`" section),
  README.md (Common commands line), tests/COVERAGE.md (unit test list entry).
  Deviations from brief: cmd.exe launched as `/c` with MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL=* (deterministic
  even if the user exports MSYS_NO_PATHCONV, where `//c` would break); installer also gets those so /S is not
  path-converted. Linux output dir is dist/linux-unpacked on x64 (no arch suffix) and linux-arm64-unpacked on
  arm64 (verified in app-builder-lib getArchSuffix); mac likewise dist/mac vs dist/mac-arm64. The --no-sandbox
  rule also fires on Debian's unprivileged_userns_clone=0. Linux launch refuses without DISPLAY/WAYLAND_DISPLAY.
  Open risks (not verified, no Windows/mac here): electron-builder npmRebuild:true may try to node-gyp node-pty
  (its prebuilds are pty.node/conpty.node, not prebuildify node.napi.node names), which needs VS Build Tools on
  Windows / Xcode CLT on macOS; `open -a` may not pass HARNESS_DESKTOP_ROOT through LaunchServices; NSIS reuses a
  registry InstallLocation if Harness was installed elsewhere before (reported, not handled); no lock against
  two concurrent installs; electron-updater in the installed app may self-update from desktop-latest and diverge
  from the stamp.
