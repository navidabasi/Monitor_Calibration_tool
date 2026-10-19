# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: on the repository page choose
**Security → Report a vulnerability**. Please don't open a public issue for
security problems.

Useful reports include steps to reproduce, the version
(`monitor-calibration --version`) and what an attacker gains.

## What's in scope

- The local server (`monitor_calibration/server.py`): DNS rebinding, CSRF,
  path traversal, reading or changing a session from another website.
- The hosted API (`api/v1/`) and database rules (`supabase/migrations/`):
  bypassing validation, rate limits or proof-of-work, poisoning the shared
  corrections, reading data that isn't returned by design.
- Anything that would send photos or personal data off the user's computer.

## How the project defends itself

**Local app**
- Binds to `127.0.0.1` only.
- Rejects any request whose `Host` header isn't this server (DNS rebinding) or
  whose `Origin` is foreign (CSRF).
- API calls need a random token created on every launch.
- Sends `frame-ancestors 'none'` so other sites can't frame it.
- Treats answers from the shared database as untrusted: schema and numeric
  ranges are checked before use.

**Hosted API and database**
- Users never talk to the database. Every table has row level security enabled
  and no policies for `anon` or `authenticated`.
- The service-role key, HMAC secrets and the Turnstile secret exist only as
  server environment variables.
- Strict schema validation (unknown fields rejected), a 64 KB body limit and
  generic error messages.
- Proof-of-work, rate limits and Turnstile make bulk submissions expensive.
- The server recomputes every result from raw patch readings. A hand-edited
  summary is rejected.
- Submissions are quarantined until they agree with other independent
  contributors. Corrections use medians and one vote per contributor.
- IP addresses are never stored raw. Rate limiting uses a salted hash that
  rotates daily and is deleted within 48 hours.

**Supply chain**
- Releases are published to PyPI from GitHub Actions with trusted publishing
  (no stored token) and build attestations.
- Dependabot, CodeQL, `gitleaks` and `pip-audit` run in CI.
