# Handoff

For the next AI model (or engineer) working on this project. Read this first, then
`docs/IMPLEMENTATION_PLAN.md` (the plan and its **Status** section, which lists what
was built, what differs from the plan and what has never been run) and `README.md`.

Written 2026-10-19. The project owner is the user in the conversation; their GitHub
user is `navidabasi`.

## What this project is

A tool that calibrates a monitor with a phone camera. The user photographs on-screen
charts in RAW (DNG); Python analyzes them and says which monitor settings to change.
It runs locally with one terminal command (`uvx monitor-calibration`, or `run.ps1` /
`run.sh` on machines without Python), serving a page on `127.0.0.1` and opening
Chrome or Edge. On top of that sits an optional **shared database** (Vercel API +
Supabase) where people anonymously contribute measurements so that others get
accurate results without a reference monitor.

## State at handoff

| Phase | State |
| --- | --- |
| 0 Release readiness | Done. CI runs on GitHub (all OS jobs green except any still being fixed, see below). PyPI publishing is **not set up yet**. |
| 1 Device detection | Done. Phone tags (allow-list), EDID, Setup UI, Window Management API mapping. |
| 2 Local correction profiles | Done. |
| 3 Backend (API, SQL, review tool) | Done and **deployed** (Vercel + Supabase, see "Live deployment"). |
| 4 Client integration | Done, incl. consent, Settings, contributions, deletion. |
| 5 Hosted website (Pyodide) | **Not started.** |
| 6 Second device as reference | **Not started.** |
| 7 Monitor knowledge | Done and deployed: suggested settings, reference profiles, coverage. |

130 tests pass (`python -m unittest discover -s tests`, about one minute, Python 3.14
on Windows; syntax is checked for 3.10). Nothing is committed: the git repository is
one level up and is not the project's repository. This folder is its own repository root
(`.github/` must be at the root).

## Hard constraints (from the plan; do not break)

- **No photos ever leave the computer.** Only derived numbers.
- **Allow-lists, never block-lists** (`monitor_calibration/privacy.py`, mirrored in
  `api/v1/_lib/schema.py` and documented in `docs/DATA.md`). A new shared field needs all
  three, plus `docs/PRIVACY.md` if it changes what is true about privacy.
- **Privacy wording must be true.** Use "We don't use your data to identify you, build a
  profile of you, or track you." Never promise "we never sell or share".
- **The client is untrusted.** The server recomputes everything from raw patch readings
  (`submission.verify`) and never believes a client-computed result.
- **No secrets in the repo or the client.** Service-role key, HMAC secrets and the
  Turnstile secret exist only as server env vars (`.env.example` lists names).
- **No standalone app** (no PyInstaller, Electron, installers). Pure Python, prebuilt
  wheels only on Windows, macOS (Intel and Apple Silicon) and Linux. `requires-python`
  is capped `<3.15` on purpose.
- **The local app works fully offline.** Declined or undecided sharing means no request
  to the shared database at all (lookups included).
- **Nothing is suggested without enough independent samples.** The owner asked for this
  explicitly; see "Thresholds" below.

## Map of the code

Local app (`monitor_calibration/`):

| File | Role |
| --- | --- |
| `cli.py`, `server.py` | The command and the local server (Host/Origin checks, per-launch token). `server.SESSION` is a **module global**: one server per process. |
| `raw_io.py`, `detect.py`, `chart.py` | RAW load, chart detection, patch measurement |
| `analyze.py` | Results and advice; `correction_matrix`, method `profile` |
| `colorimetry.py`, `icc.py` | Color math, ICC parsing |
| `tiff_meta.py` | Reads exposure and the allow-listed capture tags straight from the DNG (by tag ID only) |
| `privacy.py` | **The allow-lists and cleaning.** Start here for anything shared |
| `edid.py` | Connected monitors (Windows: registry + WMI; macOS: ioreg; Linux: sysfs). Never returns serials |
| `profiles.py` | Corrections saved on this computer |
| `submission.py` | Builds a shared session; on the server, `verify` and `plausibility` |
| `shared.py` | The app's client for the API; checks every answer; proof of work via `_pow.py` |
| `web/` | The page (`app.js` is one big file; no build step) |

API (`api/v1/`): thin Vercel entry files (`challenge.py`, `submissions.py`, ...) call
`_lib/glue.make_handler(route)`. All logic is in `_lib/`: `service.py` (endpoints,
`handle()`), `schema.py` (strict validator), `proof.py`, `hashing.py` (IP hashes),
`aggregate.py` (corrections: accept, quarantine, freeze), `recipes.py` (suggested
settings), `refprofiles.py` (measured reference profiles), `coverage.py`, `store.py`
(`MemoryStore`, used by tests and the dev server), `supabase_store.py` (the real one).

Other: `supabase/migrations/` (three files), `scripts/review.py` (admin),
`scripts/dev_api.py` (in-memory API on localhost), `scripts/coverage.py`,
`vercel.json`, `.github/workflows/`.

## How the shared data works (the part that is easy to get wrong)

- **A phone correction** is `K = A_ref . inv(A_cam)`, scaled to keep the luminance of
  white (`analyze.correction_matrix`). Applying it: `A = K . A_cam`. It is **not** `A_ref`,
  which scales with the reference monitor's brightness. Keyed by phone (camera model,
  lens, RAW type, camera app) and by screen light type (monitor model, else technology).
  The correction belongs to the **reference** monitor's screen type; it is looked up by
  the **target** monitor's screen type.
- **Submission** = capture tags + monitor fields + up to three charts (reference, white,
  latest bright target) + a client summary used only to catch edited payloads.
  `submission.build` (client) and `submission.verify` (server) share `analyze_shared`.
- **Lifecycle:** `pending` -> hourly job (`aggregate.run`, Vercel cron) -> `accepted` or
  `quarantined`. A key needs 3+ contributors (different network prefixes) on 2+ days that
  agree (200 K for a D65 white, 0.12 in any matrix element). One vote per contributor hash.
  Tripwires freeze a key (shift over 150 K, or over 20 pending in a day); `scripts/review.py`
  unfreezes.
- **Settings samples** (`settings_samples`): the person answers "did you calibrate it?" on
  the Results page and presses Share; the RGB values go with a full verified session, and
  the **server measures the white/dE itself**. Stored by EDID manufacturer + product code +
  picture mode, not by monitor row, so names can't create fake models.
- **Reference samples** (`reference_samples`): primaries and white from `icc` / `manual`
  references only. A session that used the database's values is kind `database` and never
  counts. A bug where every ICC reference was reported as `manual` was fixed in
  `shared.build_payload` (the kind is computed before the spec is cleaned).
- Deleting by code removes the submission and both sample kinds, then re-aggregates.

### Thresholds (constants; the owner cares about these)

| What | Rule | Where |
| --- | --- | --- |
| Phone correction served | 3+ contributors | `aggregate.MIN_CONTRIBUTORS` |
| Suggested settings shown | 5+ contributors, 2+ days, who said they calibrated and measured 2 dE or less, ratios agree within 4 points; baseline (settings left as they were) needs the same | `recipes.py` |
| Reference profile shown | 5+ contributors, 2+ days, spread 0.01 in x and y | `refprofiles.py` |
| The app's own floor | 5 contributors, whatever the server says | `shared.MIN_RECIPE_CONTRIBUTORS`, `MIN_PROFILE_CONTRIBUTORS` |

Below the thresholds the API answers `{"found": false}` and **reveals no counts**.

## Live deployment (2026-10-19)

- **API:** `https://monitor-calibration.vercel.app/api/v1` (a Vercel project linked to this
  repository; every push to `main` deploys). Functions run in Frankfurt (`fra1`), next to
  the database. Vercel Authentication is **off** on purpose: it's a public API.
- **Database:** a Supabase project in eu-central-1, with the three migrations applied
  (verified: RLS on and no policies, the public key is refused on every table and function,
  the helper functions work, `pg_cron` runs `purge_expired()` hourly).
- **Environment variables** (Vercel, production and preview): `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, `POW_SECRET`, `IP_SALT_SECRET`, `CRON_SECRET`; the secrets are
  write-only. `SITE_ORIGIN` and `TURNSTILE_SECRET` stay empty until the hosted website
  (phase 5) exists.
- **Hobby plan limits:** the aggregation cron in `vercel.json` runs **once a day** (03:05 UTC),
  because Hobby doesn't allow hourly. On Pro, change it back to hourly.
- **`DEFAULT_API_URL`** in `monitor_calibration/shared.py` points at it, so the shared
  database is on by default (the app still asks for consent before sending anything).
- Lessons from getting it deployed: Vercel limits `includeFiles` to 256 characters; it finds a
  Python function by the literal `class handler(BaseHTTPRequestHandler)` in each file under
  `api/` (so entry files define the class and call `glue.serve`); `.vercelignore` keeps
  `pyproject.toml` (scipy, rawpy) out of the API bundle; the MCP/API tokens used here could not
  read build logs, only `errorMessage` on a deployment.
- **Verified live** with the real app client and synthetic data: a full submission with
  settings (server measured 6526 K and 0.24 dE itself), nonce replay refused (403), schema
  violation (400), wrong content type (415), cron refused without the secret and run with it,
  lookups and coverage answering `found: false`, and deletion by code removing exactly the
  matching rows. The stored rows held only allow-listed fields, 32-byte hashes and
  month-or-day dates. The test data was removed afterwards.

## Not verified (be honest about these when reporting)

- PyPI trusted publishing: not configured (the name `monitor-calibration` was free on
  2026-10-19). The one-line starters download the repository archive until the first release.
- Aggregation across several real contributors (accept, quarantine, freeze) runs only
  against the in-memory store in tests; the live test had a single contributor.
- Turnstile: tested with a stub; no site key exists.
- EDID on macOS and Linux, and `window.getScreenDetails()` on a multi-monitor setup: not
  run. EDID reading was verified on one Windows machine.
- The UI was exercised in the built-in browser pane against an in-memory API, not on a
  second platform or phone-size layout.

## Placeholders and open decisions

- No custom domain: the API lives on `monitor-calibration.vercel.app`. Moving to a domain
  means changing `DEFAULT_API_URL` and, for the hosted site, `SITE_ORIGIN`.
- The GitHub repository is `navidabasi/Monitor_Calibration_tool`.
- Turnstile is assumed for the hosted site; the owner hasn't confirmed.
- Hosted-mode limitation to document (plan phase 5): a browser can't be started with
  `--force-color-profile=srgb`.

## What to do next

1. **Release:** set up PyPI trusted publishing (see `.github/workflows/release.yml`), tag
   `v0.1.0` and check `uvx monitor-calibration` on a clean machine; then switch the default
   source in `run.sh` and `run.ps1` from the repository archive to the PyPI package.
2. **Phase 5, hosted website:** the same page, with analysis in a Web Worker. The plan says
   run `monitor_calibration` under Pyodide and swap `rawpy` for a browser RAW decoder
   (evaluate LibRaw as WebAssembly first, then UTIF.js). `raw_io` must choose its backend at
   import time. Verify against real DNGs (iPhone Lightroom and ProRAW, Samsung Pro, Pixel,
   Open Camera) and add golden vectors compared to 1e-6 in CI with Playwright. This is
   the biggest unknown; the analysis must stay one codebase.
3. **Phase 6, second device as reference:** QR pairing in hosted mode, an opt-in `--lan`
   mode serving only the chart page with a token in the URL, reference kind `device`
   (already in the allow-list).
4. Smaller: a search box for picking a reference monitor that isn't connected (the
   `monitors/search` endpoint exists in the API, but the app has no client code or UI for
   it; Setup only offers detected monitors); a hosted Coverage page; real-device checks of
   `noteScreen()`.

## Working notes (lessons from this session)

- **Run tests:** `.venv\Scripts\python -m unittest discover -s tests` from this folder. The
  venv has numpy, scipy, rawpy and `build`. Tests render synthetic DNGs (`tests/synth.py`),
  so no real photos are needed. `tests/test_api.py` has the `Base` and `Helpers` classes the
  other API tests build on (importing them into another module is fine; they define no tests
  themselves). Inheriting a class that *has* tests re-runs them: use a helper base instead.
- **Try the whole thing locally:** `python scripts/dev_api.py` (in-memory API on :8787), then
  `MONCAL_API_URL=http://127.0.0.1:8787/api/v1 python calibrate.py --no-browser --port 8766 --data <folder>`.
  Make test DNGs with `tests/synth.write_dng`. The dev API's fake clock is how tests simulate
  "different days"; HTTP callers can set `X-Real-IP` to look like different networks.
- **Negative lookup results are cached for 10 minutes** in the app (`shared._cache`);
  toggling sharing in Settings clears it (`shared.forget_lookups`).
- **Shell:** on this Windows machine, long Bash heredocs containing quotes or apostrophes
  sometimes fail to parse. For anything non-trivial write a script file with the Write tool
  and run it. Don't stop processes by matching their command line with a pattern: it killed
  the tool's own shell once. Stop servers by port instead.
- **The built-in browser's screenshots time out when its window is hidden**; `get_page_text`
  and `javascript_tool` still work and were enough to verify the UI.
- `analyze.py`'s older tests use exact numbers; when changing analysis, run the whole suite.
- `app.js` has no build step and no framework; keep it that way. Match the surrounding
  style: short comments that say why, no new dependencies unless asked.
- Don't commit or push unless the owner asks. Don't publish or deploy on your own.
