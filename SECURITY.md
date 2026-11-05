# Security policy

**This is a final release of a concluded project and is not maintained.**
There is no supported version, no patch cadence, and no commitment to fix
reported vulnerabilities (see "Maintenance status" in the README).

You may still report an issue — a report with a clear reproduction may be
read and may prompt a fix or a warning note, but no response time is
promised. Open a GitHub issue, or use GitHub's private vulnerability
reporting if the issue would put users of forks at risk.

Deployment expectations: the web UIs bind loopback-only by default, the
shared-secret `WEB_UI_TOKEN` gate is not real authentication, nothing here
serves TLS, and the Streamlit console has no authentication at all. None of
these services are designed to face an untrusted network — see
`pm/core/web_auth.py` and `docs/release-audit/04-security.md` before
exposing anything beyond localhost.
