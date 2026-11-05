#!/usr/bin/env bash
# Install (or re-install) the fleet's systemd units and cron entries.
#
# Why this exists
# ---------------
# The entire operational topology — which arm runs with which flags, which
# profile, which interval, which services are retired — lived only in
# ~/.config/systemd/user and one user crontab. None of it was in version
# control. Losing the home directory, or moving to another machine, meant
# reconstructing two dozen unit files from prose in docs/file-map.md.
#
# deploy/systemd/ and deploy/cron/ are now the source of truth. This script
# copies them into place; `--capture` goes the other way, snapshotting the
# live configuration back into the repo after you have changed something by
# hand.
#
# Usage
#   deploy/install.sh --dry-run     # show what would change (default)
#   deploy/install.sh --install     # copy repo -> ~/.config, daemon-reload
#   deploy/install.sh --capture     # copy ~/.config -> repo (then commit)
#
# Deliberately does NOT enable or start anything. Which services should be
# running is a decision with money attached — several units in here are
# retired on purpose, and a script that enabled everything it found would
# restart theses that were measured dead.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/deploy/systemd"
UNIT_DST="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
CRON_FILE="$REPO_DIR/deploy/cron/crontab.txt"

# The unit files in the repo are machine-neutral: they reference the
# placeholder repo path below instead of any real home directory. Install
# renders the placeholder to THIS checkout; capture renders real paths back
# to the placeholder so a personal username never lands in version control.
PLACEHOLDER_REPO="/home/USER/Coding/kalshi-trader"
render_install() { sed -e "s|$PLACEHOLDER_REPO|$REPO_DIR|g" -e "s|/home/USER|$HOME|g" "$1"; }
render_capture() { sed -e "s|$REPO_DIR|$PLACEHOLDER_REPO|g" -e "s|$HOME|/home/USER|g" "$1"; }

MODE="dry-run"
case "${1:-}" in
    --install) MODE="install" ;;
    --capture) MODE="capture" ;;
    --dry-run|"") MODE="dry-run" ;;
    *) echo "usage: $0 [--dry-run|--install|--capture]" >&2; exit 2 ;;
esac

echo "repo units : $UNIT_SRC"
echo "system dir : $UNIT_DST"
echo "mode       : $MODE"
echo

if [[ "$MODE" == "capture" ]]; then
    mkdir -p "$UNIT_SRC" "$(dirname "$CRON_FILE")"
    n=0
    for u in "$UNIT_DST"/*.service "$UNIT_DST"/*.timer; do
        [[ -e "$u" ]] || continue
        name="$(basename "$u")"
        # Only ours — do not vacuum up unrelated user units.
        case "$name" in
            fade-*|back-*|near-close-*|mention-*|candle-*|control-plane*|\
            baseball-*|wnba-*|tennis-*|kalshi-ws*|reactive-*|arb-scan*|\
            news-capture*|mlb-book-poller*|pre-position*|monologue-record*|\
            polymarket-*)
                # (longshot-*/favorite-*/closegame-*/crypto-capture*/consensus-*
                #  are retired — their unit files live in attic/deploy/systemd/.
                #  fade-retail/fade-props retired 2026-08-23; fade-nearclose/
                #  back-nearclose retired 2026-08-24 — the fade-*/back-* globs
                #  now catch nothing live, kept for a future arm.)
                render_capture "$u" > "$UNIT_SRC/$name"; n=$((n+1)) ;;
        esac
    done
    if crontab -l >/dev/null 2>&1; then
        crontab -l | sed -e "s|$REPO_DIR|$PLACEHOLDER_REPO|g" -e "s|$HOME|/home/USER|g" > "$CRON_FILE"
    fi
    echo "captured $n unit(s) and the crontab into the repo."
    echo "Review with 'git diff deploy/' before committing — unit files can"
    echo "contain paths and flags that changed for a reason."
    exit 0
fi

# ─── dry-run / install ────────────────────────────────────────────────────
changed=0
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
for f in "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer; do
    [[ -e "$f" ]] || continue
    name="$(basename "$f")"
    dst="$UNIT_DST/$name"
    render_install "$f" > "$tmp"
    if [[ ! -e "$dst" ]]; then
        echo "  NEW      $name"; changed=$((changed+1))
        [[ "$MODE" == "install" ]] && { mkdir -p "$UNIT_DST"; cp "$tmp" "$dst"; chmod 644 "$dst"; }
    elif ! cmp -s "$tmp" "$dst"; then
        echo "  MODIFIED $name"; changed=$((changed+1))
        if [[ "$MODE" == "install" ]]; then
            cp -p "$dst" "$dst.bak.$(date -u +%Y%m%dT%H%M%SZ)"
            cp "$tmp" "$dst"; chmod 644 "$dst"
        fi
    fi
done

if [[ "$changed" -eq 0 ]]; then
    echo "  units already match the repo."
fi

if [[ "$MODE" == "install" ]]; then
    systemctl --user daemon-reload
    echo
    echo "daemon-reload done. Nothing was enabled or started — that is deliberate."
    echo "Bring a service up explicitly, e.g.:"
    echo "    systemctl --user enable --now fade-nearclose.service"
    echo
    echo "Cron is NOT installed automatically either (it would clobber entries"
    echo "added since the snapshot). Review and apply by hand:"
    echo "    diff <(crontab -l) $CRON_FILE"
    echo "    crontab $CRON_FILE"
else
    echo
    echo "Dry run only. Re-run with --install to apply."
fi
