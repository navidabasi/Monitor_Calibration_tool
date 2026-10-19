# Implementation plan: public release, shared device database, hosted website

This plan is written for the engineer (or model) implementing it. It describes
what to build, in which order, the constraints that must not be broken, and
how each phase is verified. Read it fully before starting.

Current state: a working local tool, packaged as the Python package
`monitor_calibration` with the terminal commands `monitor-calibration` and
`moncal` (`pyproject.toml`). `uvx monitor-calibration` starts it and opens the
browser. It analyzes phone RAW photos of on-screen charts and gives
calibration advice. 27 tests pass (synthetic DNG pipeline, local server
security, CLI). See `README.md` for how it works.

---

## 1. Goals

1. Publish the project on GitHub and PyPI so anyone can run it locally with
   **one terminal command** (`uvx monitor-calibration`) that opens the app in
   their browser. No standalone desktop app or installer.
2. Run a hosted version on Vercel that people can use without installing anything.
3. A shared database (Supabase) of **phone corrections**: once enough people
   with the same phone model have measured with a reference monitor, others with
   that phone get accurate white point readings **without** a reference monitor.
4. Also collect knowledge about **monitors**: measured colors of reference
   monitors per model, and typical starting settings per monitor model and
   picture mode.
5. Detect the phone, reference monitor and calibrated monitor automatically,
   and add unknown ones to the database.
6. Collect nothing that could identify, track or harm a user.
7. Be robust against fake submissions, scraping, brute force and common attacks.

## 2. Hard constraints (do not break these)

- **No photos ever leave the user's computer.** Only derived numbers are sent.
- **No personal data is stored**: see the allow-list in section 5. Anything not
  on the allow-list is dropped. Never use a block-list.
- **No secrets in the client or the repository.** The Supabase service-role key,
  HMAC secrets and the Turnstile secret exist only as server environment variables.
- **Users never talk to Supabase directly.** All access goes through the API
  (section 7). Every table has row level security enabled with no policies for
  `anon` or `authenticated`, which means deny everything.
- **The client is untrusted.** The server recomputes every result from the raw
  patch readings and never trusts a client-computed correction.
- **The app does not show users the payload it sends.** What is collected is
  documented publicly in `docs/DATA.md` instead. Ask once, at first use, whether
  to share (section 6).
- **Privacy wording must be true.** Use: "We don't use your data to identify
  you, build a profile of you, or track you." Do not write "we never sell or
  share your data" or similar promises.
- **The local version must keep working fully offline** (except for optional
  database lookups and contributions).
- **No standalone app.** Local use is a terminal command that starts the local
  server and opens the browser. Don't add PyInstaller, Electron or installers.
  Keep the package pure Python with dependencies that have wheels on Windows,
  macOS (Intel and Apple Silicon) and Linux.
- **Must work on machines without Python.** `run.ps1` / `run.sh` install uv if
  needed, and uv downloads its own Python. Every dependency must resolve to
  **prebuilt wheels** on every supported platform (no compiling on user
  machines). `requires-python` is capped (`<3.15`) because uv picks the newest
  Python it can download and rawpy lags behind new Python releases. Raise the
  cap only after rawpy (and numpy, scipy) publish wheels for the new version
  and the CI platform check passes.

## 3. Background: what the shared correction is

A session with a reference monitor produces a camera→XYZ matrix `A_ref` (see
`monitor_calibration/analyze.py::_reference_matrix`). The DNG also embeds the phone maker's
matrix `A_cam` (see `monitor_calibration/raw_io.py::_camera_to_xyz`). The phone's error on
screen light is captured by `A_ref`, expressed in the camera's native RGB.

The correction depends on **two keys**:

1. **Phone**: `UniqueCameraModel` (for example `iPhone17,2 back camera`) +
   lens (`LensModel` / focal length) + RAW type (Bayer vs LinearRaw) + capture
   app family (Lightroom, Apple ProRAW, Open Camera, …). Units of one model share
   sensor and filters, so this transfers between people.
2. **Screen light type**: the spectrum of the measured screen. Prefer the exact
   monitor model; fall back to a technology class:
   `woled`, `qdoled`, `oled_rgb` (phones, tablets), `ips_ksf` (wide-gamut IPS
   with narrow red phosphor), `ips_wled`, `va_qd`, `va_wled`, `mini_led_qd`, `unknown`.

Store per submission: `A_ref` normalized so `A_ref @ cam_white_of_reference`
has Y = 1, plus `A_cam` and the reference white camera RGB. The aggregate per
key is the element-wise **median** of normalized `A_ref` matrices from accepted
submissions, with spread statistics (section 8).

When a user has no reference shot, look up the best key in this order:
phone + exact monitor model → phone + screen technology → no correction (fall
back to `A_cam`, marked "rough" as today).

Averaging many contributors also averages out each contributor's reference
monitor error, so a well-populated entry can be more accurate than one
reference monitor.

## 4. Architecture

```
                              ┌──────────────────────────┐
  Local: `uvx monitor-        │  Vercel                  │      Supabase (Postgres)
  calibration` (Python) ──────┼─► /api/v1/* (Python fns) ├────► tables, RLS deny-all
  photos stay local           │   holds service-role key │      pg_cron jobs
                              │  static site             │
  Hosted site (browser) ──────┼─► same API               │
  analysis in WebWorker       └──────────────────────────┘
  photos stay local
```

- **Local mode**: the `monitor-calibration` command (`monitor_calibration/cli.py`)
  starts the Python server on `127.0.0.1` and opens Chrome or Edge. It calls
  the public API from Python (server to server) for lookups and contributions.
  Sessions live in the user data folder (`cli.user_data_home()`), never inside
  the installed package.
- **Hosted mode**: the same frontend (`monitor_calibration/web/`), but analysis
  runs **in the browser** in a Web Worker. Run the existing
  `monitor_calibration` package with **Pyodide** (numpy and scipy are available
  there) and replace `rawpy` with a browser RAW decoder that yields the same
  arrays. `rawpy` isn't available in Pyodide, so make `raw_io` pick its decoder
  backend at import time. Evaluate LibRaw compiled to WebAssembly first, then
  UTIF.js (TIFF/DNG with lossless JPEG). Verify against real DNGs from iPhone
  (Lightroom DNG and Apple ProRAW), Samsung Pro mode, Pixel and Open Camera.
  Keep one analysis codebase (Python) for both modes.
- **API**: Vercel Python serverless functions under `api/v1/`, importing the
  numpy-only modules of `monitor_calibration` (`analyze.py`, `colorimetry.py`,
  `chart.py`) to recompute submissions server-side. Keep `scipy` and `rawpy` out
  of the API bundle.
- RAW files are 10–25 MB and Vercel function bodies are limited to a few MB,
  which is another reason analysis must stay client-side.

### Repository layout (target)

```
pyproject.toml          package + `monitor-calibration` / `moncal` commands (exists)
monitor_calibration/    package: analysis, local server, CLI (exists)
  web/                  frontend shipped inside the package (exists)
  web/worker/           hosted-mode analysis worker (Pyodide + RAW decoder)
calibrate.py            run from a checkout without installing (exists)
api/v1/                 Vercel Python functions
supabase/migrations/    SQL schema, RLS, functions, cron
scripts/                admin tools (review queue, re-aggregate), use env secrets
tests/                  unit, golden vectors, API tests
docs/                   ARCHITECTURE.md, DATA.md, PRIVACY.md, SECURITY.md, …
.github/workflows/      CI, CodeQL, secret scanning, Dependabot config
vercel.json             headers, rewrites, cron
.env.example            names of required env vars, no values
```

## 5. Data: what is collected

### Allow-list (the only fields ever sent)

**Phone and capture** (from DNG tags):
`Make`, `Model`, `UniqueCameraModel`, `LensModel`, `FocalLength`, `FNumber`,
`ExposureTime`, `ISOSpeedRatings`, `Software` (app name and version only),
`DNGVersion`, RAW type, CFA pattern, `BlackLevel`, `WhiteLevel`,
`ColorMatrix1/2`, `ForwardMatrix1/2`, `CalibrationIlluminant1/2`,
`AsShotNeutral`, image width and height, orientation.

**Monitors** (reference and calibrated): EDID manufacturer ID (3 letters),
EDID product code, EDID model name, manufacture **year** only, EDID chromaticity
coordinates, screen technology (from the database or a fixed list), picture mode
(fixed list: `srgb`, `standard`, `user`, `native`, `cinema`, `game`, `other`),
resolution, and the user-chosen reference kind (`srgb`, `p3`, `adobe`, `icc`,
`manual`, `device`). For ICC references, send only the parsed primaries and
white point, never the profile file or its description text.

**Measurements**: the 15 patch readings per chart (signal, glare, noise, pixel
count, position), the 4 corner-mark readings, chart framing values (fraction,
keystone, angle, offset), exposure normalization, quality check results, the
client-computed summary (for comparison only), app version, analysis version
(`RESULT_VERSION`), and **year-month** of the session.

### Never collected

Photos or thumbnails; GPS or any location; exact dates or times; serial numbers
(EDID serial, `BodySerialNumber`, `LensSerialNumber`, any `*Serial*` tag);
`Artist`, `Copyright`, `OwnerName`, `CameraOwnerName` (Lightroom writes the
user's name here when set); MakerNote blocks (Apple MakerNote contains content
identifiers); `ImageUniqueID`, `RawDataUniqueID`, `OriginalRawFileName`; file
names or paths (they contain the Windows user name); any free text typed by
the user (monitor nicknames); hostnames, user names, OS account info; IP
addresses (see below); cookies, device IDs, fingerprints, or any ID that links
one user's submissions to each other.

### IP addresses

Vercel processes the client IP to serve requests. The API must:
- never write the raw IP to the database or to application logs;
- for rate limiting, use `HMAC(ip, daily_salt)` with a salt that rotates daily,
  keep rate-limit rows for at most 48 hours, then delete them;
- for "independent contributor" checks (section 8), store
  `HMAC(ip /24 for IPv4 or /48 for IPv6, monthly_salt)` on the submission,
  then null it out after 60 days (pg_cron job). Old salts are deleted, so
  hashes can't be reversed or linked across months.
- Vercel log retention: keep the default minimum, don't add log drains that
  store IPs. Disclose in `PRIVACY.md` that hosting providers process IPs
  transiently for delivery and security.

### Deletion without identity

The client generates a random 128-bit deletion code per submission, keeps it
locally (shown in Settings → "Your contributions"), and sends only
`SHA-256(code)`. `POST /api/v1/submissions/delete` with the code deletes the
matching rows and triggers re-aggregation.

## 6. Consent and user experience

- First launch (local and hosted): one dialog, not pre-selected:
  "Help everyone calibrate without a reference monitor: share anonymous
  measurements from your sessions. No photos, no personal data. [What's shared]
  [Share] [Don't share]". "What's shared" links to `docs/DATA.md` on GitHub.
  The choice can be changed in Settings at any time.
- If sharing is on, contribute automatically after a session that passes the
  quality checks (section 8). No payload preview in the app.
- Show database status on the Results page: "Using the shared correction for
  iPhone 16 Pro Max on WOLED (12 contributors, ±120 K)" or "No shared
  correction for your phone yet. A reference shot would make you the first
  contributor."
- Settings → "Your contributions": list of local deletion codes with a
  Delete button each.

## 7. API (Vercel, `/api/v1`)

All endpoints: JSON only, `Content-Type` checked, request body ≤ 64 KB,
schema-validated (pydantic or msgspec in strict mode: unknown fields rejected,
no NaN or Infinity, string length and enum limits, bounded arrays), generic
error messages (no stack traces), CORS allowing only the hosted site origin.
The local app calls from Python, so it has no Origin and doesn't need CORS.

| Endpoint | Purpose | Protection |
|---|---|---|
| `POST /challenge` | Returns a proof-of-work challenge `{nonce, difficulty, expires, sig}`. `sig = HMAC(secret, nonce‖difficulty‖expires)`. Stateless. | Rate limit 30/h per IP hash |
| `POST /submissions` | Accepts one session's measurements (section 5) plus challenge solution, plus Turnstile token when sent from the hosted site. | Verify sig and expiry, reject reused nonces (store nonce hash for its lifetime), verify PoW (SHA-256 with N leading zero bits, about 1–2 s on a laptop, adjustable), verify Turnstile (hosted), rate limit 10/day per IP hash, full validation and server recompute (section 8). Insert as `pending`. Returns `{status: "received"}` only, never data. |
| `GET /corrections` | Query by exact phone key (+ monitor model or screen technology). Returns the aggregate for that key only: matrix, contributor count, spread, version. | Rate limit 60/h per IP hash. No list or bulk endpoint. Only `accepted` aggregates with ≥ 3 independent contributors. |
| `GET /monitors/search?q=` | Typeahead for monitor models: name, EDID code, screen technology. | Min 3 chars, max 10 results, only `verified` monitors, rate limit 120/h |
| `POST /submissions/delete` | Body: deletion code. Deletes matching submissions. | Rate limit 20/h; codes are 128-bit, so guessing is infeasible |

Nothing returns raw submissions, counts per user, or timestamps.

## 8. Anti-abuse and data quality

You cannot authenticate an open-source client, so assume an attacker has the
code and can send any payload. Defense in depth:

1. **Cost per submission**: proof-of-work plus Turnstile (hosted) plus rate limits.
2. **Server recompute**: rebuild `A_ref` from the submitted reference patch
   readings and reference kind, using the same `monitor_calibration` code. Recompute gamma,
   additivity, gray tint and white point. Reject if the client summary
   disagrees beyond tolerance (catches hand-edited payloads).
3. **Physical plausibility** (reject or quarantine):
   - patch signals positive, ordered gray ramp, no clipping flags;
   - reference additivity < 6 %, reference gamma 1.9–2.6, reference worst gray tint < 4 ΔE;
   - chart fraction 0.2–0.45, keystone < 1.1;
   - `A_ref` well-conditioned (condition number below a bound), implied white
     correction vs `A_cam` within ±2000 K and ±0.02 Duv;
   - exposure normalization consistent with `ExposureTime` × ISO;
   - **embedded `ColorMatrix` matches the known matrix for that
     `UniqueCameraModel`** once 3 trusted submissions establish it. Fabricated
     phone models and matrices fail here.
4. **Quarantine**: new submissions are `pending`. A pg_cron or Vercel cron job
   (hourly) accepts them only if they're consistent with the current median for
   their key, or, for a new key, once ≥ 3 submissions from distinct monthly IP
   hashes on different days agree within tolerance.
5. **Robust aggregation**: element-wise median of normalized matrices; one
   vote per monthly IP hash per key; report spread (MAD) and the CCT spread it
   implies for a D65 white.
6. **Tripwires**: if an aggregate would shift by more than 150 K in one run, or
   a key gets more than 20 pending submissions in a day, freeze that key and
   flag it for admin review (`scripts/review.py`).
7. **New names**: phones or monitors not yet in the database are created as
   `pending` and appear in search or lookups only after verification (≥ 3
   independent submissions), so spam or offensive names never show up publicly.

## 9. Security checklist

**Supabase**
- RLS enabled on every table; no `anon` or `authenticated` policies; the
  service role is used only by the API.
- Postgres functions: `SECURITY INVOKER` by default; if `SECURITY DEFINER` is
  needed, set `search_path = ''` and schema-qualify everything.
- No storage buckets (nothing to store).
- MFA on the Supabase, Vercel and GitHub accounts; least-privilege collaborators.
- Point-in-time recovery or daily backups; migrations only through
  `supabase/migrations/` in review.

**Vercel / API**
- Secrets only as Vercel env vars (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
  `POW_SECRET`, `IP_SALT_SECRET`, `TURNSTILE_SECRET`). Nothing secret in
  the frontend or the Python package, and no secrets in any public-prefixed variable.
- Headers on all responses (`vercel.json`): strict `Content-Security-Policy`
  (`default-src 'self'`; `script-src 'self' 'wasm-unsafe-eval'` + Turnstile
  origin; `connect-src 'self'`; `frame-ancestors 'none'`), HSTS,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Permissions-Policy` allowing only `window-management=(self)`.
- No third-party analytics or trackers. If usage numbers are needed, use
  cookieless, aggregate-only analytics.
- Dependencies pinned with lock files and hashes; Dependabot; `pip-audit` in CI.

**GitHub**
- Secret scanning with push protection; `gitleaks` in CI and pre-commit; CodeQL.
- Branch protection on `main`: required reviews and passing CI.
- `SECURITY.md` with a private vulnerability-reporting channel (GitHub
  security advisories).
- Publish to PyPI only from GitHub Actions with **trusted publishing** (OIDC, so
  no PyPI token is stored anywhere) and build attestations. Releases are
  created from signed tags; nobody uploads from a laptop.

**Local app** (fix in phase 0)
- Reject requests whose `Host` header isn't `127.0.0.1:<port>` or
  `localhost:<port>` (DNS rebinding).
- Require a random per-launch token on every state-changing request: inject it
  into `index.html` at serve time, send it as an `X-MC-Token` header; also
  reject requests with a foreign `Origin` (CSRF from other websites).
- Keep binding to `127.0.0.1` only. The phase 6 LAN mode is an explicit opt-in
  serving only the chart page, with a token in the URL.
- Treat database responses as untrusted: validate schema and numeric ranges
  before use; never evaluate anything from them.

## 10. Device detection

- **Phone**: parse the allow-listed DNG tags (local: extend `raw_io`; hosted: in
  the worker). Show "iPhone 16 Pro Max · main camera · Lightroom DNG" in Results.
- **Monitors, local mode**: read EDID.
  - Windows: `HKLM\SYSTEM\CurrentControlSet\Enum\DISPLAY\*\*\Device Parameters\EDID`
    (only currently connected devices; cross-check with WMI `WmiMonitorID`).
  - macOS: `ioreg -lw0 -r -c AppleDisplay` / `IODisplayEDID`.
  - Linux: `/sys/class/drm/*/edid`.
  - Parse manufacturer ID, product code, name, year and chromaticity. Discard
    the serial immediately after reading.
- **Which screen showed which chart**: Chrome Window Management API
  (`window.getScreenDetails()`, one permission prompt). When a chart opens,
  record `currentScreen.label` for that shot's role and match it to the EDID list.
  Hosted mode has no EDID, so use the label plus database search plus user
  confirmation.
- **Screen technology and picture mode**: from the database by model; if
  unknown, ask once with the fixed list. Remember per monitor locally.

## Status (2026-10-19)

Built and tested in this repository: the rest of phase 0 (files and workflows),
phases 1 to 4 (device detection, local profiles, backend code, client
integration) and phase 7 (suggested settings, reference profiles, coverage).
130 tests pass on Python 3.14 (the 27 from before plus 103 new).
Not built: phases 5 and 6.

What has **not** been exercised, because it needs accounts or services this
repository can't reach:

- GitHub Actions: the workflows are written but have never run (there is no
  GitHub repository yet; the git repository one level up has no commits and
  `.github/` must sit at the root of whichever repository the project ends up in).
- PyPI: the name `monitor-calibration` was free on 2026-10-19. Trusted
  publishing has to be set up on pypi.org first (see `release.yml`).
- Supabase and Vercel: nothing is deployed. The SQL parses (checked with
  pglast) but has not been applied to a database; the API logic runs against an
  in-memory copy of the store (`MemoryStore`), and `SupabaseStore` is tested
  against a fake HTTP transport, so the PostgREST filters, RPC names and column
  names are checked for shape only. Run the migration and a staging deploy
  before trusting it. Vercel's `includeFiles` and rewrites in `vercel.json` are
  likewise unverified.
- Turnstile: the server-side check is tested with a stub; no site key exists.
- Window Management API (`window.getScreenDetails()`) and EDID reading on
  macOS and Linux: written, not run on real multi-monitor setups. EDID reading
  was verified on this Windows machine.
- `DEFAULT_API_URL` in `monitor_calibration/shared.py` is empty (no domain yet;
  the GitHub owner `navidabasi` is filled in everywhere). Until a domain exists the shared database is inactive and the app never asks for consent.

Decisions that differ from the text below, and why:

- **The shared matrix is a correction, not `A_ref`.** `A_ref` scales with the
  reference monitor's brightness, so medians across contributors would mix
  scales. The app and the server compute `K = A_ref . inv(A_cam)`, scaled to keep
  the luminance of white (`analyze.correction_matrix`), and aggregate and apply
  that: `A = K . A_cam`. `A_ref`, `A_cam` and the reference white are still
  stored per submission. Keys include the app family because `A_cam` depends on
  the camera app.
- **A hand-written strict validator** (`api/v1/_lib/schema.py`) replaces
  pydantic or msgspec: same rules (unknown fields rejected, finite numbers,
  bounded strings and arrays), no dependency in the serverless bundle.
- **Salts live in the database** (`salts` table), chosen at random per day and
  month and deleted by the scheduled cleanup, which is what makes old hashes
  unlinkable. The secrets from the environment are mixed in as well.
- **Aggregation runs as an hourly Vercel cron** (Python, `aggregate.py`);
  `pg_cron` only does the deletions. `corrections` rows always carry the screen
  technology, so a frozen technology key also covers its monitor rows.
  `submissions.id` is a bigint rather than a uuid, and there is an extra
  `reference_screen_tech` column.
- **Charts sent:** the reference shot, the white chart and the latest bright
  target chart (three charts), not every photo.
- **Acceptance rule:** agreement means within 200 K for a D65 white and 0.12 in
  every matrix element; a new key needs 3 contributors (different network
  prefixes) on at least 2 different days.
- **Proof of work** defaults to 20 bits; the app refuses anything above 24.
- **Suggested monitor settings (part of phase 7) are built**, ahead of the rest of
  phase 7: the Results page asks whether the person calibrated the monitor, the
  RGB values and answer go with the verified session (`settings_samples`), and
  suggestions appear only for the same monitor model and picture mode with at
  least 5 independent contributors on 2 days who reached 2 ΔE or less and agree
  within 4 percentage points. Only sessions with a reference shot can contribute,
  because that's what lets the server measure the result itself. Reference
  monitor profiles and the Coverage page are still not built.
- **Reference profiles (rest of phase 7)** come only from sessions whose reference
  was an ICC profile or report values (`icc`, `manual`) and whose monitor model
  and picture mode are known. A profile is shown when at least 5 independent
  people on 2 days agree within 0.01 in x and y, and is offered, not applied. A
  session that used the database's values is recorded as kind `database` and
  never counts as a new measurement. While adding this, a bug was fixed: the app
  used to report every ICC reference as `manual`.
- **Lookups only happen when sharing is on**, so declining or not answering means
  no request to the database at all.

## 11. Phases

Each phase ends with passing tests and an updated README section.

### Phase 0: release readiness and local hardening
- ~~Local server: Host check, per-launch token, Origin check (section 9). Add tests.~~
  **Done**: `monitor_calibration/server.py` (`_allowed`, token injected into
  `index.html`, `X-MC-Token` sent by `web/app.js::api`, anti-framing headers,
  exclusive port binding on Windows) and `tests/test_server.py` (CSRF, DNS
  rebinding, missing or wrong token, framing).
- ~~One-command launch instead of a standalone app.~~
  **Done**: `pyproject.toml` (package `monitor-calibration`, commands
  `monitor-calibration` and `moncal`, web files as package data),
  `monitor_calibration/cli.py` (user data folder per OS, next free port if 8765
  is taken, Chrome/Edge/Chromium paths on Windows, macOS and Linux,
  `--port/--data/--no-browser/--version`), `python -m monitor_calibration`,
  `calibrate.py` for checkouts, `tests/test_cli.py`. Verified by installing
  into a clean environment with pip and running through `uvx --from <path>`.
- ~~Machines without Python.~~
  **Done**:
  - `run.ps1` and `run.sh`: install uv if missing, then run. `MONCAL_SOURCE`
    overrides the package source. `run.ps1` is safe to pipe into `iex` (no
    `exit`, nothing leaks into the session). `run.sh` asks for Python 3.13 on
    Intel Macs.
  - `requires-python = ">=3.10,<3.15"`.
  - A rawpy `<0.26` pin on Intel Macs only. rawpy dropped Intel Mac wheels
    after 0.25.1.
  - `monitor_calibration/tiff_meta.py` reads shutter, ISO and date from the
    TIFF tags, because rawpy 0.25 has no `raw.other`. It's also the start of
    phase 1's tag reader.
  - Verified:
    - The site runs with `UV_PYTHON_PREFERENCE=only-managed` (uv's own Python 3.14).
    - Both scripts work as files and piped, tested with real and fake uv.
    - `uv pip compile --only-binary :all:` resolves on x86_64 and ARM64
      Windows, Apple Silicon and Intel Macs (Python 3.13), and manylinux x86_64
      and aarch64.
    - The full test suite passes on Python 3.13 + rawpy 0.25.1 and on Python
      3.14 + rawpy 0.27.1.
  - **Not executed**: the branch that installs uv on a machine without it,
    because it changes the user's system. Cover it in CI on clean runners.
- `LICENSE` (MIT for code) and the `license` field in `pyproject.toml`,
  `docs/PRIVACY.md`, `docs/DATA.md`, `docs/SECURITY.md`, `docs/ARCHITECTURE.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.env.example`.
- CI on Windows, macOS (Apple Silicon and Intel runners) and Linux:
  - run `tests/`;
  - a **bare-machine smoke test**: run `run.ps1` / `run.sh` with
    `MONCAL_SOURCE=.`, `UV_PYTHON_PREFERENCE=only-managed` and isolated
    `UV_*_DIR` folders, plus `--version`. On runners without uv, this also
    covers the uv-install branch;
  - a **wheel-availability check**: `uv pip compile pyproject.toml
    --only-binary :all:` for each `--python-platform` (`x86_64-pc-windows-msvc`,
    `aarch64-pc-windows-msvc`, `aarch64-apple-darwin`, `x86_64-apple-darwin`
    with 3.13, `x86_64-manylinux_2_28`, `aarch64-manylinux_2_28`) and for the
    highest allowed Python. Run it weekly too, so a dependency release that
    drops a platform is caught before users hit it;
  - CodeQL, gitleaks and Dependabot.
- Release workflow: on a signed tag, build the sdist and wheel, run tests,
  publish to PyPI with trusted publishing, attach attestations. Check that the
  name `monitor-calibration` is free on PyPI first (pick another if not, and
  update `pyproject.toml`, the README and the CLI `prog` name).
- Confirm the repo contains no personal data: `sessions/`, `.browser-profile/`,
  `.venv/`, `build/`, `dist/` ignored; no real user photos anywhere in history.
- **Done when**: CI green on 3 OSes; `uvx monitor-calibration` works on a clean
  machine after the first release. (Cross-origin rejection and packaging are
  already tested.)

### Phase 1: device detection (local only, nothing sent)
**Status: done.** `tiff_meta.read_capture` + `privacy.clean_capture`, `edid.py`, Setup UI, `noteScreen()` in `app.js`; tests in `tests/test_capture.py` and `tests/test_edid.py`.

- DNG metadata extraction with the allow-list (unit test: a synthetic DNG with
  GPS, serial, Artist and MakerNote tags yields only allow-listed fields).
- EDID reading per OS; Setup shows detected monitors to pick from instead of
  free text (free text stays as a local-only nickname).
- Window Management API mapping of shots to screens.
- **Done when**: Setup prefills phone and monitors on Windows; the tests prove
  that disallowed tags never appear in the extracted dict.

### Phase 2: local correction profiles
**Status: done.** `profiles.py`, `analyze.correction_matrix`, method `profile`. On the synthetic phone the white error drops from 236 K to 1 K (`tests/test_profiles.py`).

- After a session with a reference, save `{phone key, screen key, A_ref, quality}`
  under `profiles/` locally.
- Sessions without shot 1 use a matching local profile and show it as the
  method ("your saved iPhone 16 Pro Max + WOLED correction").
- **Done when**: a synthetic test with reference session A, then a no-reference
  session B on the same simulated phone, measures B's white within 60 K of truth.

### Phase 3: backend
**Status: done in code and tests** (`api/v1/`, `supabase/migrations/`, `scripts/review.py`, `tests/test_api.py`, `tests/test_review.py`, `tests/test_supabase_store.py`); not deployed. Poisoning test: 5 outliers against 10 honest submissions leave the median within 30 K (18 K measured).

- `supabase/migrations/`: tables (section 12), RLS deny-all, pg_cron jobs
  (aggregate hourly, purge rate-limit rows and IP hashes, delete expired nonces).
- `api/v1/` endpoints (section 7) with validation, PoW, Turnstile,
  rate limiting and server recompute using `monitor_calibration`.
- `scripts/review.py`: list quarantined items and frozen keys, approve or reject.
- **Done when**: API tests cover invalid schema, oversize body, bad or reused
  PoW, rate-limit exhaustion, a fabricated phone matrix, a hand-edited summary,
  poisoning (5 outliers vs 10 honest submissions leave the median within 30 K),
  and deletion by code.

### Phase 4: client integration
**Status: done**, with the end-to-end check run against the in-memory API over real HTTP (`tests/test_shared.py`, `scripts/dev_api.py`) rather than a local Supabase and Vercel dev server, which weren't available. Also exercised by hand in the built-in browser.

- Consent dialog, Settings toggle, contribution after valid sessions, deletion codes.
- Lookup on Results when there's no shot 1; show contributor count and spread.
- **Done when**: end-to-end against a local Supabase (`supabase start`) and a
  local Vercel dev server.

### Phase 5: hosted website
**Status: not started.**

- Web Worker with Pyodide + RAW decoder; IndexedDB for sessions; same UI.
- `vercel.json` with headers, rewrites and cron; deploy; custom domain.
- Hosted-mode limitation to document: the browser can't be started with
  `--force-color-profile=srgb`, so the Prepare checklist must ensure the
  Windows color profile is the default sRGB one.
- **Done when**: the same synthetic DNG gives identical results (within 1e-6)
  in local Python and in the browser worker (golden vectors in CI using
  Playwright).

### Phase 6: second device as reference
**Status: not started.**

- Hosted mode: "Use a phone or tablet as reference" shows a QR code with a
  pairing URL; the second device opens the chart page full screen.
- Local mode: opt-in `--lan` serving only the chart page with a token in the URL.
- Reference kind `device` with the device model recorded (Apple and Samsung
  screens in Natural mode color-manage sRGB content).

### Phase 7: monitor knowledge
**Status: done in code and tests** (`recipes.py`, `refprofiles.py`, `coverage.py`, `scripts/coverage.py`, `tests/test_recipes.py`, `tests/test_reference_profiles.py`); not deployed. The hosted website's own Coverage page waits for phase 5; `docs/COVERAGE.md` and `GET /api/v1/coverage` exist.

- Reference monitor profiles: aggregated measured primaries per model from ICC
  references, used to replace "assume sRGB" when available.
- Starting settings per monitor model and picture mode (median slider values
  that reached under 2 ΔE).
- A public "Coverage" page (aggregate counts only) on the website and in the
  repo, generated from the database.

## 12. Database schema (sketch)

```sql
create table phones (
  id bigint generated always as identity primary key,
  unique_camera_model text not null check (length(unique_camera_model) <= 80),
  make text, model text, lens_model text, focal_length real,
  raw_type text check (raw_type in ('bayer','linear')),
  app_family text check (length(app_family) <= 40),
  embedded_color_matrix real[],          -- established from trusted submissions
  status text not null default 'pending' check (status in ('pending','verified','blocked')),
  unique (unique_camera_model, lens_model, raw_type, app_family)
);

create table monitors (
  id bigint generated always as identity primary key,
  edid_manufacturer char(3), edid_product_code int, model_name text check (length(model_name) <= 64),
  screen_tech text check (screen_tech in ('woled','qdoled','oled_rgb','ips_ksf','ips_wled','va_qd','va_wled','mini_led_qd','unknown')),
  status text not null default 'pending' check (status in ('pending','verified','blocked')),
  unique (edid_manufacturer, edid_product_code, model_name)
);

create table submissions (
  id uuid primary key default gen_random_uuid(),
  session_month date not null,                       -- first day of month only
  app_version text, analysis_version int not null,
  phone_id bigint references phones, reference_monitor_id bigint references monitors,
  target_monitor_id bigint references monitors,
  reference_kind text, target_picture_mode text,
  capture jsonb not null,          -- allow-listed DNG fields
  measurements jsonb not null,     -- patch readings, marks, framing, quality
  server_result jsonb not null,    -- recomputed A_ref, checks
  status text not null default 'pending' check (status in ('pending','accepted','rejected','quarantined')),
  contributor_hash bytea,          -- HMAC(ip prefix, monthly salt); nulled after 60 days
  deletion_hash bytea not null,    -- SHA-256 of client deletion code
  received_on date not null default current_date
);

create table corrections (
  phone_id bigint references phones, monitor_id bigint references monitors, screen_tech text,
  matrix real[] not null, contributors int not null, spread_k real, analysis_version int,
  frozen boolean not null default false, updated_on date,
  primary key (phone_id, coalesce(monitor_id, 0), coalesce(screen_tech, ''))
);

create table rate_limits (key_hash bytea, window_start timestamptz, count int,
  primary key (key_hash, window_start));
create table used_nonces (nonce_hash bytea primary key, expires timestamptz);

alter table phones enable row level security;     -- repeat for every table;
-- no policies for anon/authenticated: all access via the service role in the API.
```

Adjust types as needed (the composite primary key with `coalesce` needs a
unique index instead), but keep the privacy properties: month-only dates,
nullable expiring contributor hash, deletion hash, no IP or timestamp columns.

## 13. README requirements

The README is the front door; write it for non-developers first, with
developer sections after. Required sections, in order:

1. **Title, one-line description, screenshot or GIF** of the Results page.
2. **Use it online**: link to the website, a 30-second overview.
3. **How it works, in plain words**: the four shots, the "tinted sunglasses"
   explanation of why a reference (or a shared correction) is needed, and what
   works without one (gamma, gray tint, near-black).
4. **Accuracy and limits**: honest; reference quality, phone vs screen spectra,
   OLED notes, slider prediction caveats.
5. **What you need**: phone and app table (iPhone: Lightroom mobile;
   Android: Open Camera or Pro mode), tripod, dark room, about 45 minutes.
6. **Step-by-step guide**: Prepare checklist highlights; camera settings table
   (mode, DNG, lens, ISO, shutter, white balance, focus, grid, timer,
   orientation); framing with the grid; the 4 shots; getting RAW files to the
   computer without losing RAW (Lightroom export "Original", Telegram "File",
   USB with "Keep Originals"); reading the results; the slider preview; retaking shot 3.
7. **Reference options**: sRGB, P3, Adobe RGB, ICC, report values, second
   device, shared corrections.
8. **Shared database**: what it does, coverage link, consent, link to
   `docs/DATA.md`, deleting your contributions.
9. **Privacy** summary (true statements only, section 2) and link to `docs/PRIVACY.md`.
10. **Run it locally**:
    - the one-line start for machines without Python first (`run.ps1` /
      `run.sh`), explaining what they install and where;
    - `uvx monitor-calibration` for people who have uv, and the Intel Mac note;
    - the supported-platform table, including browser notes (Edge is built
      into Windows; Chrome or Edge recommended on macOS and Linux);
    - alternatives (pipx, pip, `python -m monitor_calibration`, `moncal`);
    - options (`--port`, `--data`, `--no-browser`, `--version`);
    - where sessions are stored per OS, and how to uninstall;
    - the Chrome color flag and why;
    - running from a source checkout.

    A first version of this section already exists in the README; replace
    `navidabasi` in its URLs.
11. **Troubleshooting**: JPEG renamed to DNG, sideways chart, overexposed
    patches, sliders locked in sRGB mode, "white hasn't changed", chart not
    found, Chrome not found.
12. **Technical details**: pipeline (decode, detection, glare, flat field,
    camera matrix, metrics), formulas (XYZ, xy, CCT/Duv, CIELAB, ΔE 2000,
    gamma, gain solve), the chart layout and ID code.
13. **Architecture**: local vs hosted vs API vs database, with a diagram.
14. **Development**: project layout, running tests, synthetic DNG generator,
    golden vectors, coding style, how to add a chart field or a check.
15. **Self-hosting the backend**: Supabase project setup, applying migrations,
    env vars from `.env.example`, Vercel deploy, Turnstile keys, cron.
16. **Security**: summary, link to `SECURITY.md`, how to report vulnerabilities.
17. **Contributing, license, acknowledgements** (LibRaw/rawpy, numpy, scipy, Pyodide).

## 14. Open decisions for the project owner

- Domain name for the hosted site (needed for CORS, CSP and Turnstile).
- GitHub owner: personal account or organization.
- Turnstile is assumed for bot protection on the hosted site; confirm, or pick
  an alternative.
- The PyPI package name, if `monitor-calibration` is already taken.
