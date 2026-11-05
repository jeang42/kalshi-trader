# Release audit — 06: First-public-release checklist

## Hard blockers (do before making the repo public)

- [x] License metadata consistent (pyproject said "Proprietary") — fixed.
- [x] No secrets/credentials/PII in tree or history — verified (02-findings S-3).
- [x] Unauthenticated services no longer bind the network by default — fixed.
- [x] Control-plane balance leak on the open `/health.json` — fixed.
- [x] Mandatory order caps enforceable regardless of caller — fixed.
- [x] **Decide the `research/*.json` question** — author chose **slim to
  derived aggregates** (2026-11-04): removed the two raw API dumps
  (`active_liquidity_programs.json`, `incentive_markets_cache.json`) and
  stripped Kalshi-authored title/rules text from `live_census.json`'s
  example records (6.9 MB → ~1.1 MB). Kept: both derived screens,
  the slimmed census, and `oi_confirmation.json` (the author's own
  statistics). NOTICE narrowed accordingly; the scripts regenerate the
  removed captures when rerun.
- [ ] Final read of `docs/` for anything the author considers private
  (the logs are already redacted to `$—`; `docs/second-machine.md` describes
  the home setup in generic terms — judged publishable, but it is personal
  context the author should sign off on).

## Strongly recommended (small, before or immediately after publishing)

- [x] Re-run `pytest`, `ruff`, `mypy` on the merge commit — done 2026-11-04
  (2133 passed / 2 skipped, ruff clean, mypy clean); CI repeats it on push.
- [x] Tag the release — `v0.2.0a1` created 2026-11-04.
- [x] Add a `SECURITY.md` stating the no-maintenance policy (one paragraph,
  mirrors README "Maintenance status") so GitHub's security tab isn't blank
  — added 2026-11-04.
- [ ] Replace the fallback tuner IPs `192.168.0.50/.51` with RFC 5737
  documentation addresses or require `HDHR_IP` to be set (S-4).
- [x] Author sign-off on the `dex` example profile name (S-5) — author chose
  removal; renamed to the neutral `user2` everywhere (code, tests, docs,
  CHANGELOG). Note for a redeploying fork: an existing `data/dex/` profile
  directory keeps working (profiles are directory-driven); only the shipped
  default-profile name changed.

## Optional improvements (fine to leave for forks)

- [ ] Vendor `htmx.min.js` instead of loading from unpkg (S-6).
- [ ] Dependency audit against current advisories (`pip-audit`) at the
  moment of release; pins are `>=` so installs resolve fresh.
- [ ] CONTRIBUTING.md / CODE_OF_CONDUCT.md if contributions are actually
  wanted (the maintenance-status section currently implies not).
- [ ] A persistent uncertain-order store if anyone ever scales the live
  path beyond a single operator (see 03-order-safety.md, accepted
  limitation).
- [ ] TLS / real authentication if any web UI must face an untrusted
  network (explicitly out of scope per web_auth.py's design notes).

## What must NOT be claimed

- Do not describe the strategies as profitable or validated — the repo's own
  research record says the opposite, and the README leans into that.
- Do not claim the live path is "verified safe for real-money trading."
  It is *safe-by-default* (off, triple-gated, canary caps, fail-closed
  guards) — a different and honest claim.
