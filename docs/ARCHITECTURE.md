# Architecture

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

## Pieces

| Piece | Where | Notes |
| --- | --- | --- |
| Analysis | `monitor_calibration/` | One Python codebase: RAW load, chart detection, colorimetry, advice |
| Local app | `cli.py`, `server.py`, `web/` | Server on `127.0.0.1`, token + Host/Origin checks |
| Device detection | `edid.py`, `tiff_meta.py` | Phone tags (allow-list) and monitor EDID |
| Local profiles | `profiles.py` | Camera corrections saved from reference sessions |
| Privacy filter | `privacy.py` | Allow-list applied before anything is sent |
| Shared database client | `shared.py` | Proof-of-work, submissions, lookups (server to server) |
| API | `api/v1/` | Vercel Python functions; recomputes every submission |
| Database | `supabase/migrations/` | RLS deny-all; all access via the service role in the API |
| Admin tools | `scripts/` | Review queue, frozen keys |

## The shared correction

A session with a reference monitor gives a camera→XYZ matrix `A_ref`. The DNG
also embeds the maker's matrix `A_cam`. The phone's error on screen light is
captured by `A_ref`. Corrections are keyed by **phone** (camera model, lens,
RAW type, capture app) and **screen light type** (monitor model, else
technology class). The shared value is the element-wise median of normalized
`A_ref` matrices from accepted submissions.

Lookup order: phone + exact monitor, phone + screen technology, then no
correction (the phone's own matrix, marked "rough").

## Suggested monitor settings

Sessions can carry the settings a person chose to share (`settings` in the
submission: calibrated or not, plus the Red, Green and Blue slider values). The
server recomputes the result from the readings, so the claimed numbers are
never trusted, and stores a row in `settings_samples` keyed by the monitor's
EDID manufacturer and product code and the picture mode. `GET
/api/v1/monitors/settings` computes the suggestion on request
(`api/v1/_lib/recipes.py`) and returns nothing unless at least 5 independent
contributors on 2 or more days, who reached 2 ΔE or less, agree within 4
percentage points. The app checks the answer again and applies its own minimum.

## Reference profiles and coverage

Sessions whose reference came from an ICC profile or report values (`icc`,
`manual`) also file the reference's primaries and white point in
`reference_samples`, keyed by the reference monitor's EDID and picture mode.
`GET /api/v1/monitors/reference` (`api/v1/_lib/refprofiles.py`) returns the median
only with at least 5 contributors on 2 or more days who agree within 0.01 in
CIE x and y. The app offers it in Setup as an alternative to "assume sRGB" and
marks the session `database` when it is used, so shared values never re-enter
the pool as if they were a new measurement. `GET /api/v1/coverage` and
`scripts/coverage.py` (writes `docs/COVERAGE.md`) report counts only.

## Trust model

The client is untrusted. The server rebuilds `A_ref` from the submitted patch
readings and rejects results that disagree with the client's summary or are
physically implausible. New submissions are `pending` until they agree with
other independent contributors. See [`SECURITY.md`](SECURITY.md).

## Not built yet

- Hosted analysis in a Web Worker (Pyodide + browser RAW decoder).
- Second device as reference.
- The hosted website's own Coverage page (the data and `docs/COVERAGE.md` exist).
