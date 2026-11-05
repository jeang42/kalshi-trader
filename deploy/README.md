# deploy/ — the fleet, under version control

The operational topology of this project — which capture daemons, paper arms
and web UIs run, with which flags, profiles and intervals — lives here rather
than only in one machine's `~/.config`. `systemd/` holds the user units,
`cron/crontab.txt` the cron entries, and the two scripts move them in and out.

## Machine-neutral paths

The committed unit files and crontab reference a **placeholder** path,
`/home/USER/Coding/kalshi-trader`, never a real home directory.
`install.sh` renders the placeholder to the actual checkout location when
installing, and renders real paths *back* to the placeholder when capturing —
so the files work on any machine and a personal username never lands in
version control.

## install.sh

```bash
deploy/install.sh --dry-run     # show what would change (default)
deploy/install.sh --install     # render + copy repo -> ~/.config, daemon-reload
deploy/install.sh --capture     # copy ~/.config -> repo (de-personalized)
```

Installing deliberately does **not** enable or start anything. Which services
should run is a decision with money attached — several units exist for
strategies this repo's own research measured dead (see
`docs/strategy-scoreboard.md`), and an installer that started everything it
found would revive them. Bring a service up explicitly:

```bash
systemctl --user enable --now candle-panel.service
```

The crontab is also never applied automatically. Render and review it first:

```bash
sed -e "s|/home/USER/Coding/kalshi-trader|$(pwd)|g" -e "s|/home/USER|$HOME|g" \
    deploy/cron/crontab.txt > /tmp/crontab.rendered
diff <(crontab -l) /tmp/crontab.rendered   # review before applying
```

## bootstrap.sh

Second-machine bootstrap: fresh clone → verified environment, each step gated
on the one before (venv from `requirements.lock.txt`, test suite, smoke
checks). See the script header and `docs/second-machine.md`.

## What the units are

- **Capture daemons** (`candle-panel`, `kalshi-ws`, `*-capture`,
  `mlb-book-poller`, …) — read-only data collection into SQLite stores.
- **Paper arms** (`reactive-*`, `pre-position`, `arb-scan`, …) — strategy
  daemons writing paper trades; real orders only behind the triple gate
  (see `docs/live-execution-adapter.md`).
- **Web UIs** (`baseball-web`, `wnba-web`, `tennis-web`, `control-plane`) —
  LAN-facing FastAPI dashboards behind a shared-secret token.
- **Ops** (`polymarket-health-digest`, `polymarket-alert@`) — health digest
  and failure notifications (ntfy).
