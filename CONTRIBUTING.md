# Contributing

Thanks for helping. Bug reports with a clear description are as useful as code.

## Set up

```bash
git clone https://github.com/navidabasi/monitor-calibration
cd monitor-calibration
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
.venv/bin/python -m unittest discover -s tests
```

## Ground rules

- **No photos, ever.** Don't attach real photos of people or places to issues,
  and don't add them to the repository. Tests use the synthetic DNG generator in
  `tests/synth.py`.
- **Privacy is a hard constraint.** Anything the app sends to the shared
  database must be on the allow-list in `monitor_calibration/privacy.py` and
  documented in `docs/DATA.md`. Never add a block-list. See
  `docs/IMPLEMENTATION_PLAN.md` section 5.
- **Keep it pure Python** with dependencies that ship prebuilt wheels on
  Windows, macOS and Linux. No installers or bundled apps.
- **Add tests** for new behavior. A change to analysis results needs a
  synthetic-DNG test.
- Match the surrounding code: small functions, short comments that explain why.

## Pull requests

1. One focused change per pull request.
2. Tests pass on your machine (`python -m unittest discover -s tests`).
3. Update `README.md` or `docs/` if behavior changes.

## Reporting security problems

Don't open a public issue. See [`docs/SECURITY.md`](docs/SECURITY.md).
