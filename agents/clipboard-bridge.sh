#!/usr/bin/env bash
#
# harness clipboard bridge — container side.
#
# Installed in the agent image and symlinked as the clipboard binaries
# opencode's TUI shells out to when it copies: xclip, xsel, wl-copy. opencode
# has no working clipboard inside the container (no display, no native tool),
# so without this its copy never leaves the container. This shim takes the
# copied text on stdin and writes it (base64, single line) to the host-shared
# bridge file named by $HARNESS_CLIPBOARD_FILE. The harness host process polls
# that file and pushes the text onto the real host clipboard. No network, no
# daemon: just one bind-mounted file. See architecture/containers.md.
#
# Only the COPY (write) direction is bridged. Paste/read invocations
# (xclip -o, xsel -o, wl-paste) are a no-op success: the host->container
# direction is intentionally not bridged, and blocking on stdin in read mode
# would hang opencode.
set -u

# Read/paste mode -> emit nothing, succeed (looks like an empty clipboard).
# wl-paste is the read tool by name; xclip/xsel read with -o/--output.
case "${0##*/}" in
    wl-paste) exit 0 ;;
esac
for _a in "$@"; do
    case "$_a" in
        -o|--output) exit 0 ;;
    esac
done

# Bridge disabled (launch without HARNESS_CLIPBOARD=1 sets no file): swallow
# stdin so opencode's copy is a silent no-op, exactly as before this shim
# existed. opencode also emits OSC 52 independently, so a terminal that
# supports it still copies regardless of this shim.
if [[ -z "${HARNESS_CLIPBOARD_FILE:-}" ]]; then
    cat >/dev/null 2>&1
    exit 0
fi

# Copy mode: stdin bytes -> base64 (single line) -> overwrite the bridge file
# in place. Truncate-write (`>`) keeps the file's inode, which a single-file
# bind mount requires; a rename would break the mount. The base64 framing
# keeps the payload one newline-free line so the host reader can decode it
# whole and stay binary/newline safe.
base64 2>/dev/null | tr -d '\n' >"$HARNESS_CLIPBOARD_FILE" 2>/dev/null || true
exit 0
