# Release audit — 06: First-public-release checklist

## Hard blockers (do before making the repo public)

- [x] License metadata consistent (pyproject said "Proprietary") — fixed.
- [x] No secrets/credentials/PII in tree or history — verified (02-findings S-3).
- [x] Unauthenticated services no longer bind the network by default — fixed.
- [x] Control-plane balance leak on the open `/health.json` — fixed.
- [x] Mandatory order caps enforceable regardless of caller — fixed.
- [ ] **Decide the `research/*.json` question** (~6.9 MB of captured Kalshi
  API output incl. verbatim rules text). Current state: kept, with a NOTICE
  clause excluding it from the Apache grant. Options: keep-with-notice
  (needs the author's comfort with Kalshi's ToS on redistribution), slim to
  derived aggregates, or delete. *This is the only open blocker-class item,
  and it is a judgment call only the author can make.*
- [ ] Final read of `docs/` for anything the author considers private
  (the logs are already redacted to `$—`; `docs/second-machine.md` describes
  the home setup in generic terms — judged publishable, but it is personal
  context the author should sign off on).

## Strongly recommended (small, before or immediately after publishing)

- [ ] Re-run `pytest`, `ruff`, `mypy` on the merge commit (CI does this).
- [ ] Tag the release (e.g. `v0.2.0a1`) so "final release" is a fixed ref.
- [ ] Add a `SECURITY.md` stating the no-maintenance policy (one paragraph,
  mirrors README "Maintenance status") so GitHub's security tab isn't blank.
- [ ] Replace the fallback tuner IPs `192.168.0.50/.51` with RFC 5737
  documentation addresses or require `HDHR_IP` to be set (S-4).
- [ ] Author sign-off on the `dex` example profile name (S-5), or rename to
  a neutral placeholder.

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
