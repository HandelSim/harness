# scripts/lib/upgrade_actions.sh
#
# Sourceable bash library implementing the four upgrade action types used by
# `harness upgrade`. The harness script reads scripts/upgrade-manifest.json,
# dispatches each entry to one of these functions, and aggregates the
# JSON-line summaries they emit on stdout. Human-readable progress logs go to
# stderr.
#
# Action types:
#   upgrade_envfile_merge       <source> <target> [dry_run]
#   upgrade_linefile_merge      <source> <target> [dry_run]
#   upgrade_directory_overwrite <source> <target> <dry_run> [preserve...]
#   upgrade_userfile_sync       <source> <target> <dry_run> [allow_prompt]
#
# All four return 0 on success, 1 on a hard error. Each emits exactly one
# JSON object on stdout summarizing what changed; the runner aggregates these
# with jq for the final upgrade report. Atomic writes via .tmp + rename are
# used everywhere so an interrupted upgrade can never leave a half-written
# config behind.

# Guard against double-source.
if [[ -n "${HARNESS_UPGRADE_ACTIONS_LOADED:-}" ]]; then
    return 0 2>/dev/null || true
fi
HARNESS_UPGRADE_ACTIONS_LOADED=1

# Same fallback treatment for the y/n prompt reader: the harness script
# defines _upgrade_confirm (reads /dev/tty, honors HARNESS_CONFIRM_FROM_STDIN)
# and we must not override it. Standalone users of this library (upgrade_test.sh)
# get this minimal stand-in, which they can override with their own stub to
# script an answer.
if ! declare -F _upgrade_confirm >/dev/null 2>&1; then
    _upgrade_confirm() {
        local prompt="$1" default="${2:-y}" ans
        if [[ "${HARNESS_CONFIRM_FROM_STDIN:-0}" == "1" ]]; then
            IFS= read -rp "$prompt" ans || ans=""
        else
            IFS= read -rp "$prompt" ans </dev/tty || ans=""
        fi
        ans=${ans%$'\r'}
        if [[ -z "${ans:-}" ]]; then
            case "$default" in
                n|N) return 1 ;;
                *)   return 0 ;;
            esac
        fi
        case "$ans" in
            y|Y|yes|YES) return 0 ;;
            *) return 1 ;;
        esac
    }
fi

# Define harness_jq as a fallback for standalone use (e.g. upgrade_test.sh
# sources us directly without the full harness script). When sourced from
# the harness script, harness_jq is already defined and we don't override.
if ! declare -F harness_jq >/dev/null 2>&1; then
    harness_jq() {
        if command -v jq >/dev/null 2>&1; then
            jq "$@"
        else
            echo "upgrade_actions: jq required for standalone use; install" >&2
            echo "upgrade_actions: it via your package manager." >&2
            return 1
        fi
    }
fi

# --- helpers ---------------------------------------------------------------

# Stderr logger with a stable prefix.
_upg_log() {
    echo "[upgrade] $*" >&2
}

# Strip CR characters from a captured string. jq on Windows Git Bash emits
# CRLF line endings; downstream comparisons like `[[ "$x" == "[]" ]]` and
# string-into-JSON splices break against "[]<CR>". No-op on Linux/macOS.
_upg_strip_cr() {
    printf '%s' "${1//$'\r'/}"
}

# Today's date as YYYY-MM-DD; used in the "Added by harness upgrade on ..."
# marker comments. Overridable via HARNESS_UPGRADE_DATE for test
# determinism.
_upg_today() {
    if [[ -n "${HARNESS_UPGRADE_DATE:-}" ]]; then
        printf '%s' "${HARNESS_UPGRADE_DATE}"
    else
        date -u +%Y-%m-%d
    fi
}

# Emit a JSON array literal from one or more positional args. Empty args
# produce `[]`. Nothing fancy: each arg is JSON-string-escaped via jq.
_upg_json_array() {
    if (( $# == 0 )); then
        printf '[]'
        return 0
    fi
    local out
    # `-Rn '[inputs]'` slurps every raw line into one array in a single jq
    # process — same result as the old `-R . | -s .` pipeline (including
    # the split-on-newline behavior), but one jq invocation instead of two.
    # On a jq-less host each invocation is a container round-trip.
    out=$(printf '%s\n' "$@" | harness_jq -Rn '[inputs]')
    _upg_strip_cr "$out"
}

# JSON string-escape a single arg.
_upg_json_str() {
    if [[ $# -eq 0 ]]; then
        printf '""'
    else
        local out
        out=$(printf '%s' "$1" | harness_jq -Rs .)
        _upg_strip_cr "$out"
    fi
}

# Move src -> dst atomically. Honors dry-run by skipping the move and
# leaving src in place (caller cleans up).
_upg_atomic_mv() {
    local src="$1" dst="$2" dry="$3"
    if (( dry )); then
        rm -f "$src" 2>/dev/null || true
        return 0
    fi
    mv -f "$src" "$dst"
}

# --- envfile_merge ---------------------------------------------------------
#
# Append new KEY=VALUE entries from <source> to <target> when KEY is absent
# in <target>. Comment block(s) preceding the source key are carried with
# the new entry so the user sees the same context they would in
# .env.example. Existing target values are NEVER modified or removed.
#
# Output: {"action":"envfile_merge","added_keys":[...],"skipped":bool,"target":"..."}
upgrade_envfile_merge() {
    local source="$1"
    local target="$2"
    local dry_run="${3:-0}"
    local added=()
    local skipped=0

    if [[ ! -f "$source" ]]; then
        _upg_log "envfile_merge: source $source does not exist; skipping"
        printf '{"action":"envfile_merge","added_keys":[],"skipped":true,"target":%s,"reason":"source_missing"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if [[ ! -f "$target" ]]; then
        _upg_log "envfile_merge: target $target does not exist; copying source verbatim"
        if (( ! dry_run )); then
            mkdir -p "$(dirname "$target")"
            cp "$source" "$target.tmp.$$"
            _upg_atomic_mv "$target.tmp.$$" "$target" "$dry_run"
        fi
        # Surface every key as a "new" addition so the runner has a complete
        # picture for the summary.
        local key
        while IFS= read -r key; do
            [[ -n "$key" ]] && added+=("$key")
        done < <(_upg_envfile_keys "$source" || true)
        # One jq call builds the whole summary object, replacing the
        # separate _upg_json_array + _upg_json_str round-trips (each a
        # container spawn on a jq-less host). Keys arrive on stdin as raw
        # lines; select(.!="") drops the spurious empty line printf emits
        # when "${added[@]}" expands to nothing.
        printf '%s\n' "${added[@]}" | harness_jq -cRn --arg target "$target" \
            '{action:"envfile_merge",added_keys:[inputs|select(.!="")],skipped:false,target:$target,created:true}'
        return 0
    fi

    # Build set of existing target keys.
    local target_keys
    target_keys=$(_upg_envfile_keys "$target") || {
        _upg_log "envfile_merge: failed to parse target $target"
        printf '{"action":"envfile_merge","added_keys":[],"skipped":true,"target":%s,"reason":"target_parse_error"}\n' \
            "$(_upg_json_str "$target")"
        return 1
    }

    # Walk source line-by-line, accumulating comment context, and on each
    # KEY= line decide whether to emit it to the append buffer (key missing
    # in target) or drop it (key already present).
    local append_buf=""
    local pending_comments=""
    local today
    today=$(_upg_today)

    local line key val
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Blank line resets the pending comment block. We keep the blank
        # line itself in the comment block so the user-visible spacing
        # carries over to the appended entries.
        if [[ -z "$line" || "$line" =~ ^[[:space:]]*$ ]]; then
            pending_comments+=$'\n'
            continue
        fi
        if [[ "$line" =~ ^[[:space:]]*# ]]; then
            pending_comments+="$line"$'\n'
            continue
        fi
        # Reject obvious multi-line continuations. Env files don't support
        # them, but a pathological source might; refuse rather than corrupt
        # the target.
        if [[ "$line" == *$'\\' ]]; then
            _upg_log "envfile_merge: source $source has line-continuation in '$line'; aborting"
            printf '{"action":"envfile_merge","added_keys":[],"skipped":true,"target":%s,"reason":"multiline_value"}\n' \
                "$(_upg_json_str "$target")"
            return 1
        fi
        # Parse KEY=VALUE.
        if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)= ]]; then
            key="${BASH_REMATCH[1]}"
            val="${line#*=}"
            if grep -Fxq -- "$key" <<<"$target_keys"; then
                # Already present — drop accumulated comments and continue.
                pending_comments=""
                continue
            fi
            # New key: emit the pending comment block + marker + the line.
            append_buf+=$'\n'
            append_buf+="# Added by harness upgrade on ${today}"$'\n'
            if [[ -n "$pending_comments" ]]; then
                append_buf+="$pending_comments"
            fi
            append_buf+="$line"$'\n'
            added+=("$key")
            pending_comments=""
        else
            # Unparseable line in source — flag in stderr and skip. We
            # intentionally don't propagate it to the target.
            _upg_log "envfile_merge: source $source has unparseable line: $line (skipped)"
            pending_comments=""
        fi
    done <"$source"

    if [[ -z "$append_buf" ]]; then
        printf '{"action":"envfile_merge","added_keys":[],"skipped":false,"target":%s}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if (( dry_run )); then
        _upg_log "envfile_merge: would add ${#added[@]} key(s) to $target: ${added[*]}"
    else
        local tmp="$target.tmp.$$"
        cp "$target" "$tmp"
        printf '%s' "$append_buf" >>"$tmp"
        _upg_atomic_mv "$tmp" "$target" "$dry_run"
        _upg_log "envfile_merge: added ${#added[@]} key(s) to $target: ${added[*]}"
    fi

    printf '%s\n' "${added[@]}" | harness_jq -cRn --arg target "$target" \
        '{action:"envfile_merge",added_keys:[inputs|select(.!="")],skipped:false,target:$target}'
    return 0
}

# Echo the KEY names from a shell-style env file, one per line. Blank lines
# and `#` comments are skipped. Inline `#` after a value is preserved as
# part of the value (env files typically don't support inline comments).
_upg_envfile_keys() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    awk '
        /^[[:space:]]*$/ { next }
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/ {
            sub(/^[[:space:]]*/, "", $0)
            n = index($0, "=")
            print substr($0, 1, n-1)
        }
    ' "$f"
}

# --- linefile_merge --------------------------------------------------------
#
# Append new entries from <source> to <target>. An entry is the substring
# before any inline `#` (with whitespace trimmed); two lines with the same
# entry but different inline comments collide and the target's existing
# entry wins. Empty/comment-only lines are skipped.
#
# Output: {"action":"linefile_merge","added_lines":[...],"warnings":[...],"target":"..."}
upgrade_linefile_merge() {
    local source="$1"
    local target="$2"
    local dry_run="${3:-0}"
    local added=()
    local warnings=()

    if [[ ! -f "$source" ]]; then
        _upg_log "linefile_merge: source $source does not exist; skipping"
        printf '{"action":"linefile_merge","added_lines":[],"warnings":[],"target":%s,"reason":"source_missing","skipped":true}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if [[ ! -f "$target" ]]; then
        _upg_log "linefile_merge: target $target does not exist; copying source verbatim"
        if (( ! dry_run )); then
            mkdir -p "$(dirname "$target")"
            cp "$source" "$target.tmp.$$"
            _upg_atomic_mv "$target.tmp.$$" "$target" "$dry_run"
        fi
        local entry
        while IFS= read -r entry; do
            [[ -n "$entry" ]] && added+=("$entry")
        done < <(_upg_linefile_entries "$source")
        printf '%s\n' "${added[@]}" | harness_jq -cRn --arg target "$target" \
            '{action:"linefile_merge",added_lines:[inputs|select(.!="")],warnings:[],target:$target,created:true}'
        return 0
    fi

    # Build associative-array-shaped lookup of target entries (the entry
    # part before any inline `#`, trimmed).
    local target_entries=$'\n'
    while IFS= read -r entry; do
        target_entries+="$entry"$'\n'
    done < <(_upg_linefile_entries "$target")

    local append_buf=""
    local today
    today=$(_upg_today)

    local line entry
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip blanks and pure-comment lines.
        if [[ -z "$line" || "$line" =~ ^[[:space:]]*$ ]]; then
            continue
        fi
        if [[ "$line" =~ ^[[:space:]]*# ]]; then
            continue
        fi
        # Strip inline comment; trim whitespace.
        entry="${line%%#*}"
        entry="${entry#"${entry%%[![:space:]]*}"}"
        entry="${entry%"${entry##*[![:space:]]}"}"
        [[ -z "$entry" ]] && continue

        if grep -Fxq -- "$entry" <<<"$target_entries"; then
            # Same entry exists in target. Compare inline annotations: if
            # the source's full line differs from any target line for the
            # same entry, emit a warning so the user can review.
            local source_full="$line"
            local target_full
            target_full=$(_upg_linefile_full_for_entry "$target" "$entry")
            local s_norm t_norm
            s_norm=$(echo "$source_full" | tr -s '[:space:]' ' ')
            t_norm=$(echo "$target_full" | tr -s '[:space:]' ' ')
            if [[ "$s_norm" != "$t_norm" ]]; then
                warnings+=("$entry: source has '$source_full' but target has '$target_full' (target preserved; review at $target)")
            fi
            continue
        fi
        append_buf+="# Added by harness upgrade on ${today}"$'\n'
        append_buf+="$line"$'\n'
        added+=("$entry")
    done <"$source"

    if [[ -z "$append_buf" && ${#warnings[@]} -eq 0 ]]; then
        printf '{"action":"linefile_merge","added_lines":[],"warnings":[],"target":%s}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if [[ -n "$append_buf" ]]; then
        if (( dry_run )); then
            _upg_log "linefile_merge: would add ${#added[@]} entry(ies) to $target: ${added[*]}"
        else
            local tmp="$target.tmp.$$"
            cp "$target" "$tmp"
            # Ensure the target ends with a newline before we append.
            if [[ -s "$tmp" ]]; then
                local last_byte
                last_byte=$(tail -c 1 "$tmp" 2>/dev/null || true)
                if [[ "$last_byte" != $'\n' ]]; then
                    printf '\n' >>"$tmp"
                fi
            fi
            printf '\n%s' "$append_buf" >>"$tmp"
            _upg_atomic_mv "$tmp" "$target" "$dry_run"
            _upg_log "linefile_merge: added ${#added[@]} entry(ies) to $target: ${added[*]}"
        fi
    fi
    if (( ${#warnings[@]} > 0 )); then
        local w
        for w in "${warnings[@]}"; do
            _upg_log "linefile_merge: WARN: $w"
        done
    fi

    # warnings is almost always empty (only set on annotation diffs), so
    # _upg_json_array short-circuits to "[]" with no jq call; added_lines
    # and the object envelope are then built in a single jq process.
    local warnings_json
    warnings_json=$(_upg_json_array "${warnings[@]}")
    printf '%s\n' "${added[@]}" | harness_jq -cRn \
        --arg target "$target" \
        --argjson warnings "$warnings_json" \
        '{action:"linefile_merge",added_lines:[inputs|select(.!="")],warnings:$warnings,target:$target}'
    return 0
}

# Echo the entry portion (pre-`#`, trimmed) of every non-empty, non-comment
# line in a line-file.
_upg_linefile_entries() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    awk '
        /^[[:space:]]*$/ { next }
        /^[[:space:]]*#/ { next }
        {
            n = index($0, "#")
            if (n > 0) {
                line = substr($0, 1, n-1)
            } else {
                line = $0
            }
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (line != "") print line
        }
    ' "$f"
}

# Echo the first full line whose entry-portion equals the given entry.
_upg_linefile_full_for_entry() {
    local f="$1" entry="$2"
    [[ -f "$f" ]] || return 0
    awk -v want="$entry" '
        /^[[:space:]]*$/ { next }
        /^[[:space:]]*#/ { next }
        {
            n = index($0, "#")
            if (n > 0) {
                line = substr($0, 1, n-1)
            } else {
                line = $0
            }
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (line == want) {
                print $0
                exit
            }
        }
    ' "$f"
}

# --- merge prechecks -------------------------------------------------------
#
# Cheap "does <target> need a merge from <source>?" predicates. Pure bash +
# awk — NO jq and NO container round-trips — so they're safe to run on every
# agent launch as a gate before the (jq-backed) merge functions above. Both
# return 0 (success) when a merge IS needed, 1 when the target is already up
# to date. The "needed" set is computed from exactly the same key/entry
# extraction the merge uses (_upg_envfile_keys / _upg_linefile_entries), so
# the precheck can never disagree with what the merge would actually add.
#
# Annotation-only differences (a linefile entry present in both files but
# with different inline `# ...` text) are NOT reported as needing a merge:
# the merge only warns about those, it never appends, so the file wouldn't
# change and the user shouldn't be prompted.

# envfile: a merge is needed if <source> has any KEY= that <target> lacks.
upgrade_envfile_needs_merge() {
    local source="$1" target="$2"
    [[ -f "$source" ]] || return 1     # nothing to merge from
    [[ -f "$target" ]] || return 0     # target missing → would be created
    local key
    while IFS= read -r key; do
        [[ -z "$key" ]] && continue
        # Anchor on '=' so a prefix key (PROXY_API) can't false-match a
        # longer one (PROXY_API_KEY=). Keys are [A-Za-z_][A-Za-z0-9_]* (per
        # _upg_envfile_keys), so they carry no regex metacharacters.
        grep -qE "^[[:space:]]*${key}=" "$target" || return 0
    done < <(_upg_envfile_keys "$source")
    return 1
}

# linefile: a merge is needed if <source> has any entry <target> lacks.
upgrade_linefile_needs_merge() {
    local source="$1" target="$2"
    [[ -f "$source" ]] || return 1
    [[ -f "$target" ]] || return 0
    local target_entries=$'\n'
    local entry
    while IFS= read -r entry; do
        target_entries+="$entry"$'\n'
    done < <(_upg_linefile_entries "$target")
    while IFS= read -r entry; do
        [[ -z "$entry" ]] && continue
        grep -Fxq -- "$entry" <<<"$target_entries" || return 0
    done < <(_upg_linefile_entries "$source")
    return 1
}

# --- directory_overwrite ---------------------------------------------------
#
# Refresh a managed directory from <source>, leaving any path inside
# <preserve> untouched. Files in target that don't exist in source are not
# removed. Initial install is harness-install.sh's job — if target is missing this
# function errors.
#
# Output: {"action":"directory_overwrite","files_updated":[...],"files_preserved":[...],"target":"..."}
upgrade_directory_overwrite() {
    local source="$1"
    local target="$2"
    local dry_run="${3:-0}"
    shift 3
    local preserve=("$@")

    if [[ ! -d "$source" ]]; then
        _upg_log "directory_overwrite: source $source does not exist or is not a directory; skipping"
        printf '{"action":"directory_overwrite","files_updated":[],"files_preserved":[],"target":%s,"skipped":true,"reason":"source_missing"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if [[ ! -d "$target" ]]; then
        _upg_log "directory_overwrite: target $target does not exist (initial install is not handled here); skipping"
        printf '{"action":"directory_overwrite","files_updated":[],"files_preserved":[],"target":%s,"skipped":true,"reason":"target_missing"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    # Compute would-update / would-preserve lists by walking the source tree
    # and bucketing each path.
    local files_updated=()
    local files_preserved=()
    local rel
    while IFS= read -r rel; do
        if _upg_is_preserved "$rel" "${preserve[@]}"; then
            files_preserved+=("$rel")
        else
            files_updated+=("$rel")
        fi
    done < <(cd "$source" && find . -type f | sed 's|^\./||')

    if (( ${#files_updated[@]} == 0 )); then
        printf '{"action":"directory_overwrite","files_updated":[],"files_preserved":%s,"target":%s}\n' \
            "$(_upg_json_array "${files_preserved[@]}")" \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if (( dry_run )); then
        _upg_log "directory_overwrite: would update ${#files_updated[@]} file(s) in $target (preserve ${#files_preserved[@]})"
    else
        if command -v rsync >/dev/null 2>&1; then
            # -I forces same-size/mtime files to be copied anyway. Without
            # it rsync skips identically-sized targets under the same
            # second, which masks legitimate content updates from a fresh
            # repo pull where mtimes are very close to the existing files.
            local rsync_args=(-a -I)
            local p
            for p in "${preserve[@]}"; do
                rsync_args+=("--exclude=$p")
            done
            rsync_args+=("$source/" "$target/")
            rsync "${rsync_args[@]}" >/dev/null 2>&1 || {
                _upg_log "directory_overwrite: rsync failed for $source -> $target"
                printf '{"action":"directory_overwrite","files_updated":[],"files_preserved":%s,"target":%s,"skipped":true,"reason":"rsync_failed"}\n' \
                    "$(_upg_json_array "${files_preserved[@]}")" \
                    "$(_upg_json_str "$target")"
                return 1
            }
        else
            # Pure-shell fallback: cp each file individually, skipping
            # preserve paths.
            local f dest_dir
            for f in "${files_updated[@]}"; do
                dest_dir=$(dirname "$target/$f")
                mkdir -p "$dest_dir"
                cp -a "$source/$f" "$target/$f"
            done
        fi
        _upg_log "directory_overwrite: updated ${#files_updated[@]} file(s) in $target (preserved ${#files_preserved[@]})"
    fi

    printf '{"action":"directory_overwrite","files_updated":%s,"files_preserved":%s,"target":%s}\n' \
        "$(_upg_json_array "${files_updated[@]}")" \
        "$(_upg_json_array "${files_preserved[@]}")" \
        "$(_upg_json_str "$target")"
    return 0
}

# True if a relative file path matches one of the preserve specs. A spec
# matches if it equals the path, is a parent directory of the path (`data/`
# matches `data/foo`), or is the bare directory name (`data` matches
# `data/foo` too).
_upg_is_preserved() {
    local rel="$1"
    shift
    local p
    for p in "$@"; do
        # Strip trailing slash for normalized comparison.
        local pn="${p%/}"
        if [[ "$rel" == "$pn" ]]; then
            return 0
        fi
        if [[ "$rel" == "$pn/"* ]]; then
            return 0
        fi
    done
    return 1
}

# --- userfile_sync ---------------------------------------------------------
#
# Offer to replace ONE user-owned data file with the tracked default it was
# seeded from. Used for the reminder's two prompt-data files
# (<install root>/reminder.md, <install root>/tool-guidance.json), which
# `harness start` seeds once and then never touches again: without this the
# shipped wording can improve for years and an install that already has a copy
# would never see it.
#
# The user's file is NEVER replaced silently. The action only speaks up when
# the two files actually differ, asks per file (defaulting to N — keep mine),
# and copies the current version to <target>.bak before overwriting. A missing
# target is seeding's job, not ours; identical files say nothing at all.
#
# Non-interactive runs (`harness upgrade --no-prompt`, no terminal) skip with
# a reason instead of deciding for the user: a silent overwrite of hand-edited
# prose is exactly what this file's whole gitignore-and-seed dance exists to
# prevent.
#
# Output: {"action":"userfile_sync","files_updated":[...],"target":"...",
#          "skipped":bool,"reason":"..."}
upgrade_userfile_sync() {
    local source="$1"
    local target="$2"
    local dry_run="${3:-0}"
    local allow_prompt="${4:-1}"
    local name
    name=$(basename "$target")

    if [[ ! -f "$source" ]]; then
        _upg_log "userfile_sync: source $source does not exist; skipping"
        printf '{"action":"userfile_sync","files_updated":[],"target":%s,"skipped":true,"reason":"source_missing"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi
    if [[ ! -f "$target" ]]; then
        # Nothing to ask about: `harness start` seeds a missing copy from this
        # same source on the next launch.
        printf '{"action":"userfile_sync","files_updated":[],"target":%s,"skipped":true,"reason":"target_missing"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi
    if cmp -s "$source" "$target"; then
        printf '{"action":"userfile_sync","files_updated":[],"target":%s,"skipped":true,"reason":"identical"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    # How different, in whole lines, so the question carries some weight.
    local delta=""
    if command -v diff >/dev/null 2>&1; then
        delta=$(diff "$target" "$source" 2>/dev/null | grep -c '^[<>]' || true)
        [[ "$delta" =~ ^[0-9]+$ ]] || delta=""
    fi
    local delta_note=""
    [[ -n "$delta" ]] && delta_note=" ($delta line(s) differ)"

    if (( dry_run )); then
        _upg_log "userfile_sync: $name differs from the shipped default${delta_note}; would ask whether to replace it"
        printf '{"action":"userfile_sync","files_updated":[],"target":%s,"skipped":true,"reason":"dry_run"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if (( ! allow_prompt )) || { [[ ! -t 0 ]] && [[ "${HARNESS_CONFIRM_FROM_STDIN:-0}" != "1" ]]; }; then
        _upg_log "userfile_sync: $name differs from the shipped default${delta_note}; keeping yours (nothing to confirm on)"
        _upg_log "userfile_sync: re-run 'harness upgrade' interactively to review it"
        printf '{"action":"userfile_sync","files_updated":[],"target":%s,"skipped":true,"reason":"not_prompted"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    _upg_log "$name differs from the shipped default${delta_note}:"
    _upg_log "  yours:   $target"
    _upg_log "  shipped: $source"
    if ! _upgrade_confirm "Replace your $name with the shipped version? Yours is kept at $name.bak [y/N] " n; then
        _upg_log "userfile_sync: keeping your $name"
        printf '{"action":"userfile_sync","files_updated":[],"target":%s,"skipped":true,"reason":"declined"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if ! cp "$target" "$target.bak" 2>/dev/null; then
        _upg_log "userfile_sync: could not write $target.bak; leaving your $name alone"
        printf '{"action":"userfile_sync","files_updated":[],"target":%s,"skipped":true,"reason":"backup_failed"}\n' \
            "$(_upg_json_str "$target")"
        return 1
    fi
    cp "$source" "$target.tmp.$$"
    _upg_atomic_mv "$target.tmp.$$" "$target" 0
    _upg_log "userfile_sync: replaced $name with the shipped default (yours: $target.bak)"
    printf '{"action":"userfile_sync","files_updated":%s,"target":%s}\n' \
        "$(_upg_json_array "$name")" \
        "$(_upg_json_str "$target")"
    return 0
}

# userfile: there is something to ask about only when BOTH files exist and
# their bytes differ. A missing target is seeding's job (see
# seed_user_data_file in `harness`), and identical files are silent.
upgrade_userfile_needs_sync() {
    local source="$1" target="$2"
    [[ -f "$source" && -f "$target" ]] || return 1
    cmp -s "$source" "$target" && return 1
    return 0
}
