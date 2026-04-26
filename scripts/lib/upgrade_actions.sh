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
#   upgrade_json_merge          <source> <target> <strategy> [dry_run]
#   upgrade_directory_overwrite <source> <target> <dry_run> [preserve...]
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

# --- helpers ---------------------------------------------------------------

# Stderr logger with a stable prefix.
_upg_log() {
    echo "[upgrade] $*" >&2
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
    out=$(printf '%s\n' "$@" | jq -R . | jq -s .)
    printf '%s' "$out"
}

# JSON string-escape a single arg.
_upg_json_str() {
    if [[ $# -eq 0 ]]; then
        printf '""'
    else
        printf '%s' "$1" | jq -Rs .
    fi
}

# True if jq is available. The four action functions all assume jq is
# present — `harness upgrade` is gated on it in the parent shell. We still
# guard the json_merge action explicitly because it is the only one that
# would silently corrupt a config without jq.
_upg_have_jq() {
    command -v jq >/dev/null 2>&1
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
        printf '{"action":"envfile_merge","added_keys":%s,"skipped":false,"target":%s,"created":true}\n' \
            "$(_upg_json_array "${added[@]}")" \
            "$(_upg_json_str "$target")"
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

    printf '{"action":"envfile_merge","added_keys":%s,"skipped":false,"target":%s}\n' \
        "$(_upg_json_array "${added[@]}")" \
        "$(_upg_json_str "$target")"
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
        printf '{"action":"linefile_merge","added_lines":%s,"warnings":[],"target":%s,"created":true}\n' \
            "$(_upg_json_array "${added[@]}")" \
            "$(_upg_json_str "$target")"
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

    printf '{"action":"linefile_merge","added_lines":%s,"warnings":%s,"target":%s}\n' \
        "$(_upg_json_array "${added[@]}")" \
        "$(_upg_json_array "${warnings[@]}")" \
        "$(_upg_json_str "$target")"
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

# --- json_merge ------------------------------------------------------------
#
# Add missing keys from source to target without overwriting any existing
# value at any depth. The only supported strategy is `add_missing_keys`. We
# treat arrays as scalars (target wins — no array extension, no element
# merging) so a user's customized array layout is never reordered or
# truncated.
#
# Output: {"action":"json_merge","added_paths":[...],"target":"..."}
upgrade_json_merge() {
    local source="$1"
    local target="$2"
    local strategy="${3:-add_missing_keys}"
    local dry_run="${4:-0}"

    if ! _upg_have_jq; then
        _upg_log "json_merge: jq not available; cannot merge $target safely"
        printf '{"action":"json_merge","added_paths":[],"target":%s,"skipped":true,"reason":"jq_missing"}\n' \
            "$(_upg_json_str "$target")"
        return 1
    fi

    if [[ ! -f "$source" ]]; then
        _upg_log "json_merge: source $source does not exist; skipping"
        printf '{"action":"json_merge","added_paths":[],"target":%s,"skipped":true,"reason":"source_missing"}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if [[ "$strategy" != "add_missing_keys" ]]; then
        _upg_log "json_merge: unknown strategy '$strategy'; skipping"
        printf '{"action":"json_merge","added_paths":[],"target":%s,"skipped":true,"reason":"unknown_strategy"}\n' \
            "$(_upg_json_str "$target")"
        return 1
    fi

    if ! jq -e . "$source" >/dev/null 2>&1; then
        _upg_log "json_merge: source $source is not valid JSON; aborting"
        printf '{"action":"json_merge","added_paths":[],"target":%s,"skipped":true,"reason":"source_invalid_json"}\n' \
            "$(_upg_json_str "$target")"
        return 1
    fi

    if [[ ! -f "$target" ]]; then
        _upg_log "json_merge: target $target does not exist; copying source verbatim"
        if (( ! dry_run )); then
            mkdir -p "$(dirname "$target")"
            cp "$source" "$target.tmp.$$"
            _upg_atomic_mv "$target.tmp.$$" "$target" "$dry_run"
        fi
        # All paths in source are "new" here.
        local paths
        paths=$(jq -c '[paths | map(if type == "number" then "[\(.)]" else "." + . end) | join("")]' "$source" 2>/dev/null || echo '[]')
        printf '{"action":"json_merge","added_paths":%s,"target":%s,"created":true}\n' \
            "$paths" \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if ! jq -e . "$target" >/dev/null 2>&1; then
        _upg_log "json_merge: target $target is not valid JSON; refusing to overwrite"
        printf '{"action":"json_merge","added_paths":[],"target":%s,"skipped":true,"reason":"target_invalid_json"}\n' \
            "$(_upg_json_str "$target")"
        return 1
    fi

    # Recursive add-missing-keys merge. Implemented as a jq filter:
    #   def add_missing(s):
    #     if (. | type) == "object" and (s | type) == "object" then
    #       reduce (s | keys_unsorted)[] as $k (.;
    #         if has($k) then
    #           .[$k] |= add_missing(s[$k])
    #         else
    #           .[$k] = s[$k]
    #         end)
    #     else .
    #     end;
    #   . | add_missing($src)
    local merged
    merged=$(jq --slurpfile src "$source" '
        def add_missing(s):
          if (type == "object") and ((s | type) == "object") then
            reduce (s | keys_unsorted)[] as $k (.;
              if has($k) then
                .[$k] |= add_missing(s[$k])
              else
                .[$k] = s[$k]
              end)
          else . end;
        . | add_missing($src[0])
    ' "$target" 2>/dev/null) || {
        _upg_log "json_merge: jq merge failed for $target; aborting"
        printf '{"action":"json_merge","added_paths":[],"target":%s,"skipped":true,"reason":"merge_failed"}\n' \
            "$(_upg_json_str "$target")"
        return 1
    }

    # Compute the list of newly-added paths by diffing target vs merged
    # path-set. A path is "added" if it exists in merged but not in target.
    # Each path is rendered as a jq-style accessor (`.foo.bar[3]`) for
    # human readability in the summary.
    #
    # Use a real temp file rather than process substitution: native Windows
    # jq.exe cannot read MSYS-style /proc/<pid>/fd/<n> paths produced by
    # bash's <(...) syntax. The temp-file form works on every platform.
    local merged_tmp
    merged_tmp="$target.merged.$$"
    printf '%s\n' "$merged" >"$merged_tmp"
    local added_paths
    added_paths=$(jq --slurpfile m "$merged_tmp" '
        def fmt: map(if type == "number" then "[\(.)]" else "." + . end) | join("");
        ($m[0] | [paths]) - [paths] | map(fmt)
    ' "$target" 2>/dev/null || echo '[]')
    rm -f "$merged_tmp"

    if [[ "$added_paths" == "[]" || -z "$added_paths" ]]; then
        printf '{"action":"json_merge","added_paths":[],"target":%s}\n' \
            "$(_upg_json_str "$target")"
        return 0
    fi

    if (( dry_run )); then
        _upg_log "json_merge: would add path(s) to $target: $(jq -r '. | join(", ")' <<<"$added_paths")"
    else
        local tmp="$target.tmp.$$"
        printf '%s\n' "$merged" >"$tmp"
        # Validate the temp file before swapping in.
        if ! jq -e . "$tmp" >/dev/null 2>&1; then
            rm -f "$tmp"
            _upg_log "json_merge: refusing to swap; tmp file is not valid JSON"
            printf '{"action":"json_merge","added_paths":[],"target":%s,"skipped":true,"reason":"tmp_invalid_json"}\n' \
                "$(_upg_json_str "$target")"
            return 1
        fi
        _upg_atomic_mv "$tmp" "$target" "$dry_run"
        _upg_log "json_merge: added path(s) to $target: $(jq -r '. | join(", ")' <<<"$added_paths")"
    fi

    printf '{"action":"json_merge","added_paths":%s,"target":%s}\n' \
        "$added_paths" \
        "$(_upg_json_str "$target")"
    return 0
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
