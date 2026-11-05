# Release audit — 04: Security review

## Network exposure (after the P0-B fixes)

| Service | Port | Bind default | Auth | Notes |
|---|---|---|---|---|
| Streamlit console (`app.py` via `run.sh`/`make run`) | 8501 | **127.0.0.1** (override: `BIND_HOST`) | **None** | Can place real orders when the live gates are enabled. Must never be exposed beyond loopback / an operator-controlled tunnel. The profile selector writes a shared file, so anyone reaching the UI can become the owner profile. |
| Control plane | 8773 | 127.0.0.1 without token; 0.0.0.0 with `WEB_UI_TOKEN`; `WEB_BIND_HOST` overrides | `WEB_UI_TOKEN` shared secret | `/health.json` is open by design and now returns liveness only (previously leaked the real balance — fixed). Dashboard (balance, fleet state) is behind the gate. |
| Baseball / WNBA / Tennis sidecars | 8770–8772 | same rule as control plane | same token | `POST /refresh` spawns a subprocess; `POST /chat/send` drives local Ollama (client-chosen model + history incl. `system` role). Both are behind the gate; keep them off untrusted networks. No order capability. |

**Token properties:** constant-time comparison; supplied via `?token=` (then
moved into an `httponly`, `SameSite=Lax` cookie, 90-day lifetime), cookie, or
`Authorization: Bearer`. One shared secret — no users, sessions, revocation,
or TLS. `SameSite=Lax` blocks cross-site POST (CSRF) when a token is set; with
no token the apps are loopback-only, which closes the old drive-by-CSRF-on-LAN
case. 401 responses don't distinguish missing from wrong tokens.

**Deployment note (breaking change):** fleets that relied on the old open
`0.0.0.0` default must now set `WEB_UI_TOKEN` (preferred) or
`WEB_BIND_HOST=0.0.0.0` in `.env`. Documented in `.env.example`, README,
`docs/file-map.md`.

## Credential handling

- Kalshi auth = API key id (env) + RSA PEM at `KALSHI_PRIVATE_KEY_PATH`;
  the code enforces `chmod 600` on the PEM and never logs key material.
- Request signing is RSA-PSS over timestamp+method+path; no secrets in URLs.
- `.env` is gitignored (and now dockerignored); `.env.example` contains only
  commented placeholders.
- No credential ever leaves the machine except to `api.elections.kalshi.com`
  (signed headers). Optional outbound: ntfy push (token optional), local
  Ollama. No telemetry of any kind.

## Secrets scan

- `detect-secrets` over the working tree: only test fixtures, placeholder
  values, and third-party `.venv-ci`/cache noise. No real secrets.
- Full git history (11 commits; already squashed to "Initial public
  release"): no `.env`, `*.pem`, `*.key`, database, or `data/` file ever
  committed; pattern grep (AWS/GitHub/Slack/OpenAI key shapes, PEM headers)
  over every commit: no hits.
- No emails, no `/home/<user>` paths, no hostnames. Remaining personal
  residue (optional cleanup, see checklist): fallback LAN IPs
  `192.168.0.50/.51` in the HDHomeRun/feed-latency modules, and the `dex`
  example profile name.

## Injection / unsafe-input review

- **SQL:** parameterized everywhere (sqlite helpers); no string-built SQL
  found with external input.
- **Subprocess:** fixed argv lists only (`systemctl show`, sidecar
  `python -m <pkg>.runner`); no shell=True with external input.
- **Deserialization:** stdlib `json` only on external data; no pickle of
  untrusted input.
- **Path handling:** data paths come from env/config, not request input; no
  request-driven file reads/writes in the web apps.
- **Debug endpoints:** none network-exposed; FastAPI docs pages are behind
  the token gate (middleware covers all paths except the two OPEN_PATHS).

## Packaging & distribution

- `pyproject.toml` now declares Apache-2.0 (was "Proprietary"); OSI
  classifier aligned; `Private :: Do Not Upload` retained on purpose (app,
  not a library; no build-system config — an sdist would sweep in `attic/`
  and `research/`). CI/Docker install from `requirements*.txt` only.
- `requirements*.txt`: plain PyPI pins; no git/local/private-index sources.
- Docker: `.dockerignore` now excludes `.env*`, `.git`, keys.
- **Rights-review item:** `research/edge_2026_10_21/*.json` +
  `research/edge_2026_10_25/*.json` (~6.9 MB) are captured Kalshi API
  responses, including verbatim market rules text. NOTICE now states they
  are Kalshi's content outside the Apache grant. Decide before release:
  keep-with-notice (current state), slim to derived aggregates, or drop.
- Sidecar pages load `htmx` from unpkg (BSD-licensed, not vendored) — no
  license issue; availability/supply-chain consideration only.
- Runtime data fetched from MLB StatsAPI / ESPN endpoints is not
  redistributed; users are subject to those services' terms at runtime.

## What this release does NOT claim

Publishing this repository as experimental software is defensible after the
fixes above. **No claim is made that it is verified safe for real-money
trading**: the live-API code paths cannot be fully validated without
production exchange access, the venue API will drift, and the project is
unmaintained (see README "Maintenance status"). The live path ships OFF by
default behind three independent gates, at canary-sized default caps.
