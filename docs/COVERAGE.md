# Coverage

Not generated yet. Once the shared database is running, `python scripts/coverage.py` writes this page: counts only
(phone models and monitor models with a verified entry, shared corrections, suggested settings and measured
reference profiles), no names and no per-person data. The same numbers are served, cached for an hour, at
`GET /api/v1/coverage`.
