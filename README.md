# Monitor calibration with a phone

A local website that walks you through calibrating a monitor with your phone's
camera. You take four RAW photos of test charts; the analyzer measures white
point, gamma, gray tint and near-black detail, and tells you which monitor
settings to change.

**Your photos never leave your computer.** If you choose to, anonymous
measurements (numbers, never photos) can be shared so that other people with
the same phone get accurate results without a reference monitor. See
[Shared database](#shared-database) and [Privacy](#privacy).

## Run it

It runs from a terminal with one command, then opens in your browser. **You
don't need Python installed**: the commands below work on a fresh Windows,
macOS or Linux machine.

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/navidabasi/monitor-calibration/main/run.ps1 | iex
```

**macOS and Linux** (Terminal):

```bash
curl -LsSf https://raw.githubusercontent.com/navidabasi/monitor-calibration/main/run.sh | sh
```

What these do (you can read [`run.ps1`](run.ps1) and [`run.sh`](run.sh) first):

1. If [uv](https://docs.astral.sh/uv/) isn't installed, install it from
   astral.sh. uv is a single small program, not written in Python. It goes
   into your user folder, so no admin rights or sudo are needed.
2. uv downloads its own private copy of Python if needed. It doesn't touch any
   Python already on your system.
3. Start the app and open it in your browser.

The second time, it starts in a couple of seconds. If you already have uv,
this is all you need:

```bash
uvx monitor-calibration
```

On an **Intel Mac**, use `uvx --python 3.13 monitor-calibration` (`run.sh`
does this for you), because the RAW reader only has ready-made builds up to
Python 3.13 there.

| System | Works |
| --- | --- |
| Windows 10/11, Intel/AMD or ARM | Yes. Edge is built in, so chart colors are exact out of the box. |
| macOS, Apple Silicon or Intel | Yes. Install Chrome or Edge for exact chart colors; Safari works but adjusts colors and the app warns you. |
| Linux, Intel/AMD or ARM, glibc 2.28+ (Ubuntu 20.04+, Debian 10+, Fedora) | Yes. Install Chrome, Chromium or Edge for exact chart colors. |
| Linux with musl (Alpine) | Not yet (no ready-made RAW reader build). |

To remove everything later: delete the app's data folder (listed below),
then run `uv cache clean` and follow uv's
[uninstall steps](https://docs.astral.sh/uv/getting-started/installation/#uninstallation).

Until the package is published on PyPI, run it straight from GitHub:

```bash
uvx --from git+https://github.com/navidabasi/monitor-calibration monitor-calibration
```

The run scripts can do the same: set `MONCAL_SOURCE` to that `git+https://…`
address before running them.

Other ways to run it, if you already have Python:

| With | Command |
| --- | --- |
| pipx | `pipx run monitor-calibration` |
| pip | `pip install monitor-calibration`, then `monitor-calibration` |
| Python module | `python -m monitor_calibration` |
| Short alias | `moncal` (same as `monitor-calibration`) |

Options:

| Option | What it does |
| --- | --- |
| `--port N` | Use port N. Without it, the app uses 8765, or the next free port if another copy is running. |
| `--no-browser` | Start the site without opening a browser window. |
| `--data FOLDER` | Keep sessions in FOLDER instead of your user data folder. |
| `--version` | Print the version. |

The command starts a local server on `http://127.0.0.1:8765` and opens Chrome
(or Edge) with `--force-color-profile=srgb`, so the charts reach the monitor
without the browser adjusting their colors. Press Ctrl+C in the terminal to stop.

Your photos and results stay on your computer, in your user data folder:

| System | Folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\MonitorCalibration\sessions` |
| macOS | `~/Library/Application Support/MonitorCalibration/sessions` |
| Linux | `~/.local/share/monitor-calibration/sessions` (or `$XDG_DATA_HOME`) |

The server only answers its own page: it checks the `Host` and `Origin`
headers and requires a token that changes on every launch, so other websites
open in your browser can't read or change your session. After restarting the
server, reload the page.

### From a source checkout

```bash
git clone https://github.com/navidabasi/monitor-calibration
cd monitor-calibration
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\monitor-calibration
```

On macOS and Linux, use `.venv/bin/` instead of `.venv\Scripts\`.
`python calibrate.py` also works from the checkout without installing; it
keeps sessions next to the code in `./sessions`.

## The four shots

| Shot | Monitor | Chart | Shutter | Purpose |
| --- | --- | --- | --- | --- |
| 1 | Reference | Bright | 1/60 s | Calibrates the camera against a factory-calibrated sRGB monitor |
| 2 | Target | White | 1/60 s | Maps lens falloff across the frame |
| 3 | Target | Bright | 1/60 s | White point, gamma, gray tint, colors. Retake after each adjustment |
| 4 | Target | Dark | 1/4 s | Near-black detail |

Shot 1 is optional. Without a reference monitor, white point and colors come
from the phone's built-in color data and are rough; gamma and near-black are
still measured well.

The reference can be any monitor whose colors are known. In Setup, pick what
it shows: sRGB (most factory-calibrated monitors), Display P3, Adobe RGB, an
ICC profile (from your own colorimeter calibration, or downloaded for your
exact model from a review site), or primaries and white typed in from a
calibration report. A profile only fits the picture mode it was made in.

## Results

The white point panel shows how far white is from D65 on a ΔE bullseye
(under 2 counts as accurate), which way it's off (warmer/cooler,
greener/magenta), and the new Red, Green and Blue slider values. Type in the
values the monitor menu shows now; the new values keep white about as bright
and come from the monitor's own measured red, green and blue.

Each chart carries a 5-bit ID code (chart type and monitor role), so photos
can be dropped in any order.

## How it measures

- RAW files are read with rawpy. The black level is subtracted in floating
  point (LibRaw would clip negative noise to zero), and 2x2 Bayer blocks are
  merged instead of demosaiced. No white balance or color matrix is applied.
- The four corner squares locate the chart; a homography maps every patch.
- Black gaps between patches measure lens glare next to each patch, which is
  subtracted. On an OLED, black emits nothing, so the gaps are pure glare.
- Shot 2 gives a per-channel quadratic lens-falloff model.
- Shot 1 gives a camera RGB → XYZ matrix, assuming the reference shows exact
  sRGB primaries and a D65 white.

Accuracy is limited by the reference monitor's own accuracy and by the camera
seeing OLED light slightly differently from IPS light. It's not a substitute
for a colorimeter.

## Why a reference (or a saved correction) is needed

A phone camera sees colors through its own tinted sunglasses: the filters over
its sensor were chosen for everyday scenes, not for the narrow red, green and
blue peaks of a screen, especially an OLED. The maker's color data inside the
DNG can be a few hundred kelvin off for screen light. Shot 1 (a monitor whose
colors you know) measures exactly how this phone sees this kind of screen.
Gamma, gray tint relative to white and near-black don't depend on it.

That measurement is a small correction on top of the phone's own color data. It
only depends on the phone (camera model, lens, RAW type, camera app) and the
kind of screen light (the monitor model, else its technology: WOLED, QD-OLED,
IPS and so on), so it can be reused:

- **On this computer.** After a good reference session the app saves the
  correction (in `profiles/` next to your sessions). Next time, a session
  without shot 1 on the same phone and kind of screen uses it, and says so on the
  Results page.
- **From other people.** With sharing on, the app asks the shared database for a
  correction contributed by others with the same phone and kind of screen.

In **Setup**, pick the monitors detected on this computer and their screen
type. The app reads each monitor's model and primaries from its EDID. The
serial number is never read.

## Shared database

If you agree (the app asks once, at first use; neither answer is pre-selected),
a session that used a reference monitor and passed the quality checks sends
anonymous measurements: the patch readings of up to three charts, the camera's
color tags, the monitor model identifiers and screen type, and the year and
month. Others with the same phone and screen type get the median correction of
at least three independent contributors.

- **Monitor settings.** After a reference session the Results page asks whether
  you calibrated the monitor or left it as it was. If you answer and press Share,
  the Red, Green and Blue values you typed are sent with that session (the server
  measures the result itself). In return, Setup and Results show **suggested
  starting values** for your monitor model and picture mode, but only when at least
  5 independent people on at least 2 days, who reached 2 ΔE or less, agree
  closely. With fewer, nothing is shown. Every unit differs, so it's a starting
  point; shot 3 still confirms it.
- **Reference profiles.** If your reference monitor's colors come from an ICC
  profile or report values, and you picked its model and picture mode, the
  primaries and white point (numbers only) are filed under that model. When at
  least 5 independent people agree, Setup offers "Use these values" instead of
  assuming exact sRGB. See [`docs/COVERAGE.md`](docs/COVERAGE.md) for what the
  database covers (counts only).
- What is sent, field by field: [`docs/DATA.md`](docs/DATA.md). It's an
  allow-list: anything not named there is dropped before sending.
- **Never sent:** photos, location, exact dates, serial numbers, owner names,
  file names, free text, IP addresses.
- **Delete anything you shared** in Settings → Your contributions. The deletion
  code is stored only on your computer; the database keeps just its hash.
- Change your mind any time in Settings. Declined or undecided means the app
  makes no request to the shared database.
- The server doesn't trust the client: it recomputes everything from the
  readings, rejects edited or implausible sessions, and only believes a
  correction once independent contributors agree ([`docs/SECURITY.md`](docs/SECURITY.md)).

Until a server is configured the feature is inactive: the app shows no
consent dialog and sends nothing. For development or self-hosting, point it
at a server with `MONCAL_API_URL` (see [Self-hosting the backend](#self-hosting-the-backend)).

## Privacy

We don't use your data to identify you, build a profile of you, or track you.
Photos are analyzed on your own computer. Hosting providers process IP
addresses briefly to deliver requests, and the app's server never stores them
(rate limiting uses a salted hash that rotates daily and is deleted within 48
hours). Details: [`docs/PRIVACY.md`](docs/PRIVACY.md).

## Security

The local server only answers its own page (see above). The hosted API is
built on the assumption that an attacker has the source: strict schema
validation, proof of work, rate limits, server-side recomputation, quarantine
and tripwires. Row level security denies every direct database access. To
report a vulnerability, use GitHub's private reporting, described in
[`docs/SECURITY.md`](docs/SECURITY.md).

## Architecture

```
  Local: `uvx monitor-calibration` (Python) ──► /api/v1/* (Vercel Python functions) ──► Supabase (RLS deny-all)
  photos stay local                              recomputes every submission              hourly cron + pg_cron cleanup
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The hosted website (analysis
in the browser), a second device as reference and the monitor knowledge pages
from `docs/IMPLEMENTATION_PLAN.md` (phases 5 to 7) are not built yet.

## Self-hosting the backend

Not needed to use the app. To run your own shared database:

1. **Supabase**: create a project, then apply `supabase/migrations/*.sql`
   (SQL editor, or `supabase db push`). Enable the `pg_cron` extension
   (Database → Extensions) and re-run the last `cron.schedule` statement if
   the migration says it wasn't enabled. No storage buckets are used.
2. **Vercel**: import the repository. `vercel.json` sets the headers, routes
   and the hourly aggregation cron. Set the environment variables named in
   [`.env.example`](.env.example) in the project settings (never in the
   repository): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `POW_SECRET`,
   `IP_SALT_SECRET`, `TURNSTILE_SECRET`, `SITE_ORIGIN`, `CRON_SECRET`.
   Generate the secrets with `python -c "import secrets; print(secrets.token_hex(32))"`.
3. **Turnstile**: create a site in Cloudflare Turnstile for the hosted site's
   domain (only the hosted site needs it; the local app pays proof of work).
4. **The app**: set `DEFAULT_API_URL` in `monitor_calibration/shared.py` to
   `https://YOUR-DOMAIN/api/v1`, or use `MONCAL_API_URL` to try it first.
5. **Review**: `python scripts/review.py list` shows quarantined submissions
   and frozen keys (needs `SUPABASE_URL` and the service-role key in your
   environment, on your own machine).

To try everything on your computer without Supabase or Vercel:

```bash
python scripts/dev_api.py                    # API with an in-memory database
MONCAL_API_URL=http://127.0.0.1:8787/api/v1 python calibrate.py
```

The Vercel and Supabase deployment itself has not been exercised from this
repository. The SQL is syntax-checked and the API logic is tested against an
in-memory copy of the database, but run the steps above in a staging project
first.

## Tests

From a source checkout with the development install above:

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```

The tests render synthetic chart photos (simulated display, camera, lens
falloff, glare, noise, Bayer mosaic), write them as real DNG files and run the
full pipeline on them. They also cover metadata extraction (a DNG full of
serial numbers, GPS and owner names must yield only allow-listed fields), EDID
parsing, saved corrections, the hosted API (schemas, proof of work, rate
limits, edited summaries, poisoning, deletion) and a full end-to-end run
between the local app and an in-memory API over real HTTP.

## Layout

- `pyproject.toml`: package definition and the `monitor-calibration` / `moncal` commands
- `monitor_calibration/cli.py`: the command: ports, data folder, opening the browser
- `monitor_calibration/server.py`: local web server
- `monitor_calibration/chart.py`: chart layout shared by the site and the analyzer
- `monitor_calibration/raw_io.py`: RAW loading
- `monitor_calibration/detect.py`: chart detection and patch measurement
- `monitor_calibration/analyze.py`: results and advice
- `monitor_calibration/colorimetry.py`: color math (CCT/Duv, CIELAB, ΔE 2000)
- `monitor_calibration/icc.py`: reading primaries and white point from ICC profiles
- `monitor_calibration/web/`: the site, shipped inside the package
- `monitor_calibration/tiff_meta.py`: reads shutter speed, ISO, date and the allow-listed camera tags from the RAW file
- `monitor_calibration/privacy.py`: the allow-lists and cleaning for everything that may be shared
- `monitor_calibration/edid.py`: connected monitors from their EDID (no serial numbers)
- `monitor_calibration/profiles.py`: camera corrections saved on this computer
- `monitor_calibration/submission.py`: builds a shared session and, on the server, re-verifies it
- `monitor_calibration/shared.py`: the app's client for the shared database
- `api/v1/`: the hosted API (Vercel Python functions; the logic is in `api/v1/_lib/`)
- `supabase/migrations/`: tables, row level security and scheduled cleanup
- `scripts/`: `review.py` (admin review queue), `coverage.py` (writes the coverage page) and `dev_api.py` (local in-memory API)
- `docs/`: `DATA.md`, `PRIVACY.md`, `SECURITY.md`, `ARCHITECTURE.md`, `COVERAGE.md`, `IMPLEMENTATION_PLAN.md`, `HANDOFF.md`
- `run.ps1`, `run.sh`: one-line start on machines without Python (install uv if needed, then run)
- `calibrate.py`: runs the app from a checkout without installing
- `tests/`: unit and end-to-end tests, including a synthetic DNG generator
- `docs/IMPLEMENTATION_PLAN.md`: plan for the public release and shared database
