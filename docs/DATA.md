# What is collected

Only if you agree to share, and only these fields. The list is an **allow-list**:
anything not on it is dropped before sending (`monitor_calibration/privacy.py`)
and again on the server (`api/v1/_lib/schema.py`). This document is the public
description of what the app sends.

## Phone and capture (from the DNG's tags)

`Make`, `Model`, `UniqueCameraModel`, `LensModel`, `FocalLength`, `FNumber`,
`ExposureTime`, `ISOSpeedRatings`, `Software` (app name and version only),
`DNGVersion`, RAW type, CFA pattern, `BlackLevel`, `WhiteLevel`,
`ColorMatrix1/2`, `ForwardMatrix1/2`, `CalibrationIlluminant1/2`,
`AsShotNeutral`, image width and height, orientation.

## Monitors (reference and calibrated)

EDID manufacturer ID (3 letters), EDID product code, EDID model name,
manufacture **year** only, EDID chromaticity coordinates, screen technology,
picture mode (one of `srgb`, `standard`, `user`, `native`, `cinema`, `game`,
`other`), resolution, and the reference you chose (`srgb`, `p3`, `adobe`, `icc`,
`manual`, `device`, `database`). For an ICC reference, only the parsed primaries and white
point are sent, never the profile or its description.

## Measurements

The 15 patch readings per chart (signal, glare, noise, pixel count, position),
the 4 corner-mark readings, chart framing values (fraction, keystone, angle,
offset), exposure normalization, quality checks, the app's own summary (for
comparison only), the app version and analysis version, and the **year and
month** of the session.

## Reference monitor profiles

If your reference monitor's colors came from an ICC profile or values you typed
from a calibration report, and you picked the monitor model and its picture mode
in Setup, the **primaries and white point** (CIE xy numbers) are also filed under
that model and picture mode. The profile file and its description text are never
sent. A reference you only described as "sRGB", or whose values came from the
shared database (`database`), adds nothing. Measured values are offered to
others only when at least 5 independent people, on at least 2 days, agree
within 0.01 in x and y; otherwise nothing is shown. Using them is the person's
choice (a button in Setup).

## Monitor settings (only if you answer the question on the Results page)

After a session with a reference monitor, the Results page asks: *did you
calibrate this monitor, or is it still at its own settings?* Only if you answer
and press Share, these are sent with that session:

- your answer (calibrated, or left as it was);
- the Red, Green and Blue values from the monitor's menu as you typed them, and
  the slider maximum (numbers only);
- the monitor's model identifiers and picture mode (already listed above).

The server measures the result itself from the shared readings (white point,
ΔE, gamma); a result you claim is never accepted. The numbers are filed under
the monitor model and picture mode, and removed if you delete that
contribution.

## When a suggestion is shown

The app only suggests settings when enough people with **the same monitor model
in the same picture mode** agree: at least 5 independent contributors on at least
2 different days, who said they calibrated it and whose measurement reached 2 ΔE
or less, with Red, Green and Blue (as ratios of the largest) within about 4
percentage points of each other. Otherwise nothing is shown, not even a count.
What people measured with their settings left as they were is added as a
typical starting point only under the same conditions.

## Never collected

- Photos or thumbnails
- GPS or any location
- Exact dates or times
- Serial numbers (EDID serial, `BodySerialNumber`, `LensSerialNumber`, any `*Serial*` tag)
- `Artist`, `Copyright`, `OwnerName`, `CameraOwnerName`
- MakerNote blocks
- `ImageUniqueID`, `RawDataUniqueID`, `OriginalRawFileName`
- File names or paths
- Free text you typed (such as monitor nicknames)
- Hostnames, user names, operating-system account information
- IP addresses (see [`PRIVACY.md`](PRIVACY.md) for transient processing)
- Cookies, device IDs, fingerprints, or any ID that links one person's
  submissions to each other

## Deletion

Each submission carries a hash of a random code that only your computer knows.
See [`PRIVACY.md`](PRIVACY.md).
