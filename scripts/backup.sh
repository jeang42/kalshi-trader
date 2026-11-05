#!/usr/bin/env bash
# Daily backup of the polymarket data directory to the spinning disk.
#
# Why
# ---
# Every piece of accumulated state (calibration log, audit log,
# settlement records, profile JSONs, SQLite stores) lives under
# data/ on the project's SSD. Disk loss = weeks of value gone.
# This script copies it nightly to /mnt/backups/kalshi-trader/
# (17 TB spinning drive — plenty of room for many snapshots).
#
# Architecture: SQLite databases are snapshotted through the online
# backup API into a persistent cold staging directory and integrity-
# checked; everything else is rsynced into the same staging dir; then
# staging is published to the dated snapshot with --link-dest against
# the previous one. Because staging is cold, unchanged databases
# hardlink instead of being re-copied — which is what makes a 90-day
# retention cost roughly one full copy plus diffs rather than 90x.
# Each snapshot is a complete browsable view of data/ at that point
# in time — easy to inspect, easy to restore from.
#
# Databases above BACKUP_MAX_SQLITE_MB (default 8 GB) are SKIPPED and
# named in the log AND in backup_status.json ("skipped"), which makes the
# health digest page — unless listed in BACKUP_SKIP_EXPECTED (default:
# the frozen 43 GB book_archive), which is noted instead. That is deliberate: one retired-thesis archive was
# ~43 GB and copying it nightly is what took the backup target past
# 2 TB. Set BACKUP_FORCE_INCLUDE=name.sqlite to override. (The old 2 GB
# cap silently dropped the live candle panel for 12 nights in Oct 2026 —
# its stale staged copy kept appearing in every snapshot.)
#
# Retention
# ---------
# Default: keep last 90 daily snapshots. With hardlinks the space
# cost is essentially "size of total churn over 90 days" not 90×
# the data size. Tunable via RETAIN_DAYS env var.
#
# Schedule
# --------
# Daily at 01:00 via cron (before logrotate at 02:00). Manual:
#     scripts/backup.sh
#
# Restore
# -------
# Snapshots are plain directories. Restore one back:
#     rsync -a /mnt/backups/kalshi-trader/2026-05-20T01:00/ data/
#
# Verify a snapshot without restoring it (runs PRAGMA integrity_check
# on every database in it — this is run automatically after each backup):
#     python -m pm.ops.backup_sqlite --verify /mnt/backups/kalshi-trader/<stamp>

set -euo pipefail

# ─── Config ───────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# python -m pm.ops.backup_sqlite resolves the pm package from the CWD;
# cron starts us in $HOME, where it does not exist. This was missed when
# R-24 moved backup_sqlite under pm/ — four nightly runs failed the
# sqlite stage silently (2026-08-25..28) before it was caught.
cd "$REPO_DIR"
SRC="$REPO_DIR/data"
# Overridable so the script can be exercised against a scratch destination
# without touching the real backup tree (see the P0 verification run).
DEST_ROOT="${BACKUP_DEST_ROOT:-/mnt/backups/kalshi-trader}"
RETAIN_DAYS="${RETAIN_DAYS:-90}"
LOG_FILE="$REPO_DIR/data/shared/backup.log"

# ─── Sanity checks ────────────────────────────────────────────────────────
if [[ ! -d "$SRC" ]]; then
    echo "ERROR: source $SRC does not exist." >&2
    exit 1
fi

if [[ ! -d "$DEST_ROOT" ]]; then
    echo "ERROR: backup destination $DEST_ROOT does not exist." >&2
    echo "Create it first: mkdir -p $DEST_ROOT" >&2
    exit 1
fi

if ! [[ -w "$DEST_ROOT" ]]; then
    echo "ERROR: $DEST_ROOT is not writable by $(whoami)." >&2
    exit 1
fi

# ─── Snapshot path ────────────────────────────────────────────────────────
STAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
SNAPSHOT="$DEST_ROOT/$STAMP"

# Find the most recent existing snapshot to use as --link-dest. If
# this is the first run, no link-dest; rsync just does a full copy.
LATEST=$(find "$DEST_ROOT" -maxdepth 1 -type d -name '20*' | sort | tail -n 1)

# ─── Run ──────────────────────────────────────────────────────────────────
{
    echo "════════════════════════════════════════════════════════════════"
    echo "  polymarket backup — $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
    echo "  source:   $SRC"
    echo "  snapshot: $SNAPSHOT"
    if [[ -n "$LATEST" ]]; then
        echo "  link-dest: $LATEST (unchanged files hardlinked)"
    else
        echo "  link-dest: (none — first snapshot, full copy)"
    fi
    echo "════════════════════════════════════════════════════════════════"

    START=$(date +%s)

    # rsync flags:
    #   -a   archive (recursive + preserve perms, times, symlinks, etc.)
    #   --delete  removed source files removed from snapshot
    #               (snapshots are point-in-time; we want them to reflect
    #               the source as it was, not accumulated cruft)
    #   --link-dest  hardlink unchanged files from previous snapshot.
    #   --exclude-from  skip transient caches that are reproducible
    #                   and would just bloat backup size.
    RSYNC_ARGS=(-a --delete)
    if [[ -n "$LATEST" ]]; then
        RSYNC_ARGS+=(--link-dest="$LATEST")
    fi

    # Skip the live log files — they're rotated by logrotate already
    # and they change every minute, defeating the hardlink benefit.
    # Also skip __pycache__ if it ever leaks in (shouldn't, but cheap to exclude).
    #
    # SQLite is excluded here and handled separately below. rsync copies a
    # database and its -wal at different instants with no idea they belong
    # together, so a file-level copy of a HOT database can be internally
    # inconsistent — and the busiest stores (the capture services write every
    # 10s) were exactly the ones most at risk. They are instead snapshotted
    # through SQLite's online backup API into a cold staging directory and
    # integrity-checked before they are copied.
    EXCLUDES=(
        "--exclude=shared/*.log"
        "--exclude=shared/*.log.[0-9]*"
        "--exclude=__pycache__"
        "--exclude=*.sqlite"
        "--exclude=*.sqlite-wal"
        "--exclude=*.sqlite-shm"
        "--exclude=*.lock"
        "--exclude=.*.tmp.*"
    )

    STATUS="ok"

    # ─── 1. Consistent SQLite snapshots into cold staging ──────────────
    # Staging persists between runs so rsync --link-dest can hardlink the
    # databases that did not change, instead of re-copying gigabytes nightly.
    STAGING="$DEST_ROOT/.staging"
    mkdir -p "$STAGING"
    SKIPPED_FILE="$STAGING/.skipped.json"
    echo '{"skipped": ["?"], "skipped_expected": []}' > "$SKIPPED_FILE"
    if ! "$REPO_DIR/.venv/bin/python" -m pm.ops.backup_sqlite \
            --data-root "$SRC" --staging "$STAGING" \
            --skipped-file "$SKIPPED_FILE"; then
        echo "  ERROR: sqlite staging reported a failure — see above"
        STATUS="FAILED"
    fi

    # ─── 2. Everything else (JSON ledgers, JSONL, corpora) ─────────────
    if ! rsync -a "${EXCLUDES[@]}" "$SRC/" "$STAGING/"; then
        echo "  ERROR: staging rsync failed"
        STATUS="FAILED"
    fi

    # ─── 3. Publish the cold staging directory as this snapshot ────────
    if ! rsync "${RSYNC_ARGS[@]}" "$STAGING/" "$SNAPSHOT/"; then
        STATUS="FAILED"
    fi

    # ─── 4. Prove it restores ──────────────────────────────────────────
    # An untested backup is a hypothesis. This opens every database in the
    # snapshot we just wrote and integrity-checks it, so a bad backup is
    # discovered now rather than during an actual recovery.
    if [[ "$STATUS" = "ok" ]]; then
        if ! "$REPO_DIR/.venv/bin/python" -m pm.ops.backup_sqlite \
                --verify "$SNAPSHOT"; then
            echo "  ERROR: the snapshot failed verification — treat it as unusable"
            STATUS="FAILED"
        fi
    fi

    ELAPSED=$(( $(date +%s) - START ))
    SNAP_SIZE=$(du -sh "$SNAPSHOT" 2>/dev/null | cut -f1 || echo '?')
    TOTAL_SIZE=$(du -sh "$DEST_ROOT" 2>/dev/null | cut -f1 || echo '?')

    echo
    echo "  $STATUS in ${ELAPSED}s"
    echo "  snapshot size (as seen): $SNAP_SIZE"
    echo "  total backup dir:        $TOTAL_SIZE"

    # ─── Retention ─────────────────────────────────────────────────────
    # Delete snapshots older than RETAIN_DAYS. -mindepth 1 -maxdepth 1
    # so we only touch the snapshot directories themselves, not stuff
    # inside them.
    if [[ "$STATUS" = "ok" ]]; then
        DELETED=0
        while IFS= read -r -d '' OLD; do
            rm -rf "$OLD" && DELETED=$((DELETED + 1))
        done < <(find "$DEST_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' \
                 -mtime "+$RETAIN_DAYS" -print0)
        if [[ "$DELETED" -gt 0 ]]; then
            echo "  retention: pruned $DELETED snapshot(s) older than $RETAIN_DAYS days"
        fi
    fi

    # Machine-readable outcome for pm/ops/health_digest.py — a failed or
    # stale backup should page, not hide in a rotated log.
    # The sqlite stage writes {"skipped": [...], "skipped_expected": [...]};
    # spliced in as the tail of the status object. If that stage never ran,
    # the "?" placeholder above makes the digest page rather than stay green.
    SKIPPED_JSON=$(cat "$SKIPPED_FILE" 2>/dev/null || echo '{"skipped": ["?"]}')
    printf '{"stamp": "%s", "status": "%s", "elapsed_s": %d, "finished_at": "%s", %s\n' \
        "$STAMP" "$STATUS" "$ELAPSED" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${SKIPPED_JSON#\{}" \
        > "$SRC/shared/backup_status.json"

    echo
} 2>&1 | tee -a "$LOG_FILE"
