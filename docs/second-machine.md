# Standing the system up on a second machine (P3/R-26)

The disaster-recovery / migration runbook. The environment itself is proven
reproducible two ways — `deploy/bootstrap.sh` (fresh clone → lock-pinned venv
→ full gate) and `make docker-check` (the same proof inside `python:3.11-slim`
with an empty `data/`) — but a *machine* is more than the environment. This
page is the rest.

**Why not "just containerize the fleet":** production's orchestrator is
systemd user units + linger (self-staggering at boot), its log sink is
journald, its monitoring is the `polymarket-alert@` OnFailure hook, and its
deployment tool is `deploy/install.sh`. Containerizing nine daemons on a
single machine would re-implement all of that to get back to where we are —
and the audio stack (HDHomeRun on the LAN, PulseAudio, GPU faster-whisper)
does not belong in a container anyway. The container's job here is narrower:
prove the environment stands up from nothing, forever, as an artifact instead
of a habit (non-API gotcha #7).

## 0. What is NOT anywhere else — read this before the disk dies

Two things exist **only on the original machine**. They are gitignored
(correctly) and deliberately NOT in the nightly `data/` backup:

| Secret | Location | Recovery if lost |
|---|---|---|
| `.env` | repo root | Recreate from `.env.example` + memory. The two that matter: `NTFY_TOPIC` (alerting; pick a new secret topic and re-pair the phone) and the `KALSHI_*` trio. |
| Kalshi API private key | `~/.config/kalshi/private_key.pem` (0600) | **Cannot be re-downloaded.** Kalshi shows a private key once at creation. If it is gone, generate a NEW API key on kalshi.com and update `KALSHI_API_KEY_ID`. |

Decide deliberately whether to copy these to the second machine / offline
storage — that is a security call this runbook does not make for you.

## 1. Prerequisites on the new machine

- Linux with systemd user sessions; `loginctl enable-linger <user>` (this is
  what starts the fleet at boot with nobody logged in).
- `git`, `make`, Python **3.11+** (the lock was generated on 3.11; 3.13 is
  what the original box runs — both pass the gate).
- Reachable backup medium (the original backs up to
  `/mnt/backups/kalshi-trader/`, `--link-dest` hardlinked, 90-day
  retention).
- Optional, per subsystem — the core runs without all of these:
  - **Ollama** (`OLLAMA_API_BASE`, default localhost:11434) — LLM feeds/deep
    analysis idle without it.
  - **NVIDIA GPU + faster-whisper** — the mention/audio stack only.
  - **HDHomeRun on the LAN** — `pm/mention/hdhomerun.py` expects the tuner at
    its configured IP; monologue recording only.

## 2. Environment

```bash
git clone <origin> polymarket && cd polymarket
./deploy/bootstrap.sh        # venv from the LOCK + full gate + unit dry-run
```

Green gate = the environment is right. The script stops there on purpose —
everything below is operator work.

## 3. Data

```bash
# newest snapshot; each is a complete hardlinked tree
ls -1t /mnt/backups/kalshi-trader/ | head -3
rsync -a /mnt/backups/kalshi-trader/<STAMP>/ data/
.venv/bin/python -m pm.ops.backup_sqlite --verify /mnt/backups/kalshi-trader/<STAMP>
```

Notes:
- The snapshot excludes reproducible caches and `shared/*.log`; SQLite files
  in it were taken with the online-backup API and pass `integrity_check` at
  backup time — the `--verify` above re-proves it at restore time.
- `data/.active_profile` restores with the tree; the arms pin their own
  profile per cycle regardless.
- A store at a HIGHER schema version than the code refuses to open (fails
  closed) — restore onto a checkout at least as new as the backup.

## 4. Secrets

Recreate `.env` (section 0). Until it exists: the web UIs run **ungated**
(`WEB_UI_TOKEN` unset logs a loud warning), alerting is silent, and
everything money-adjacent is paper — `KALSHI_LIVE_ENABLED` defaults false and
`live_gates` reports NOT ARMED. That failure direction is correct; don't
rush this step to make warnings go away.

## 5. Fleet

```bash
bash deploy/install.sh --install     # units → ~/.config/systemd/user (never enables)
diff <(crontab -l) deploy/cron/crontab.txt   # then, if sane:
crontab deploy/cron/crontab.txt

# the enabled set as of 2026-08-24 — cross-check against the original:
#   systemctl --user list-unit-files --state=enabled
systemctl --user enable --now \
    near-close-capture mention-capture candle-panel advisory-capture \
    control-plane baseball-web wnba-web tennis-web
systemctl --user enable --now polymarket-health-digest.timer \
    monologue-record@kimmel.timer monologue-record@tonight.timer
```

Daemons stagger themselves at boot (`ExecStartPre` sleeps); a burst of
`resolved=N` on first cycles is the arms catching up, not a fault.

## 6. Verify — the four standard commands

```bash
python -m live_gates                 # expect: NOT ARMED (until .env says otherwise)
python health_digest.py --print      # expect: "No problems detected" after ~30 min
python -m oos_ledger --status        # rule-freeze clocks came over with data/
systemctl --user list-units --state=failed   # expect: empty
```

Also point the backup somewhere real again: `scripts/backup.sh` writes to
`BACKUP_DEST_ROOT` (default `/mnt/backups/kalshi-trader`) — a second
machine without that mount silently has no backups.

## Continuous proof

- `make docker-check` — rebuilds the clean-room image and runs the gate.
- CI's `test-lock` job installs from `requirements.lock.txt` (not
  `requirements.txt`) and runs the suite — so the lock can't rot between the
  weekly `pip-audit` and the day you actually need it.
