#!/usr/bin/env bash
#
# sync_status.sh
#
# Every INTERVAL seconds: (1) git pull, so this machine's local status/*.json always reflects
# every OTHER machine's latest progress too, not just its own; then (2) runs scan_status.py and
# commits+pushes ONLY this machine's own status/<machine>.json (parsed from scan_status.py's own
# "wrote ..." stdout line, so it always matches whatever machine name scan_status.py actually
# used - see its docstring - rather than reimplementing hostname-derivation here). Other
# machines' json files are pulled in but never added/committed/pushed by this machine. This is the
# "auto-commit+push step" README.md's "まだやっていないこと" #2 describes as not yet wired up;
# this script is that wiring, meant to run alongside train_auto.sh on each of the 3 machines
# (not instead of it - scan_status.py only reads THIS machine's local results/, so each machine
# needs its own copy of this loop running).
#
# A failed iteration (network down, merge conflict, etc.) is logged and skipped - it never kills
# the loop, since this is meant to be left running unattended for hours/days.
#
# Usage:
#   ./sync_status.sh                 # loop forever, 1800s (30min) between iterations
#   ./sync_status.sh 300             # loop forever, 300s (5min) between iterations
#   ./sync_status.sh 300 --once      # single iteration, then exit (for testing/cron)
#
# To run in the background and keep logs:
#   nohup ./sync_status.sh 1800 > sync_status.log 2>&1 &
set -uo pipefail

INTERVAL="${1:-1800}"
ONCE=false
[[ "${2:-}" == "--once" ]] && ONCE=true

# Resolve paths relative to this script's own directory, not $PWD, so `git`/`uv run` below
# always target this repo regardless of where the script is invoked from (matches
# scan_status.py's own config.yaml-relative-to-itself approach).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || { echo "FATAL: cannot cd into $SCRIPT_DIR" >&2; exit 1; }

if [[ ! -f config.yaml ]]; then
    echo "FATAL: config.yaml not found. Copy config.yaml.example to config.yaml and set repo_root first." >&2
    exit 1
fi

log() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

run_once() {
    local branch
    branch="$(git rev-parse --abbrev-ref HEAD)"

    # Pull first, unconditionally, every iteration - this is what keeps the OTHER machines'
    # status/*.json fresh locally even on cycles where this machine has nothing new to push
    # (previously this only ran right before a push, so a quiet machine never saw anyone else's
    # updates). --autostash guards against local status/ edits (there shouldn't be any -
    # status/*.json is machine-written, not hand-edited) getting in the way of the rebase.
    log "pulling latest status from remote..."
    if ! git pull --rebase --autostash -q origin "$branch"; then
        log "git pull failed (network down?) - continuing with local state"
    fi

    log "scanning local progress..."

    # Capture scan_status.py's stdout (while still showing it live) so we can pull out its
    # "wrote <path>" line below - that's the one file we're allowed to touch this iteration.
    local scan_log
    scan_log="$(mktemp)"
    if ! uv run python scan_status.py --report | tee "$scan_log"; then
        log "scan_status.py failed, skipping this iteration"
        rm -f "$scan_log"
        return 1
    fi

    local wrote_line
    wrote_line="$(grep -m1 '^wrote ' "$scan_log" || true)"
    rm -f "$scan_log"

    if [[ -z "$wrote_line" ]]; then
        log "could not find scan_status.py's 'wrote ...' line in its output, skipping"
        return 1
    fi

    # Reduce to just the filename, tolerating either / (Mac/Linux) or \ (Windows) separators in
    # the path scan_status.py printed, then re-anchor it under status/ - this is what keeps the
    # commit scoped to THIS machine's own json, never whatever other machines' files git pull
    # may have brought in under status/.
    local status_file="${wrote_line#wrote }"
    status_file="${status_file##*/}"
    status_file="${status_file##*\\}"
    local status_rel="status/${status_file}"

    if [[ ! -f "$status_rel" ]]; then
        log "expected $status_rel to exist after scan, skipping"
        return 1
    fi

    git add -- "$status_rel"

    if git diff --cached --quiet -- "$status_rel"; then
        log "no change in $status_rel, nothing to push"
        return 0
    fi

    local host
    host="$(hostname)"

    if ! git commit -m "status: update ${host} $(date -u +%Y-%m-%dT%H:%M:%SZ)" -q -- "$status_rel"; then
        log "git commit failed, skipping this iteration"
        return 1
    fi

    # No pull here - already pulled at the top of this function. If another machine pushed in
    # the meantime this push is simply rejected; logged and retried (pull included) next
    # iteration rather than retried inline, to keep this function's flow linear.
    if ! git push -q origin "$branch"; then
        log "git push failed, will retry next iteration"
        return 1
    fi

    log "pushed ${status_rel} for ${host}"
    return 0
}

trap 'log "interrupted, exiting"; exit 0' INT TERM

if [[ "$ONCE" == true ]]; then
    run_once
    exit $?
fi

log "starting sync loop (interval=${INTERVAL}s) in $SCRIPT_DIR"
while true; do
    run_once
    sleep "$INTERVAL"
done
