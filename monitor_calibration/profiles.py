"""Camera corrections saved on this computer, so a later session without a reference shot is still accurate.

A session with a reference monitor teaches the app how this phone sees a kind
of screen (analyze.correction_matrix). It's saved under the phone and the
screen: the exact monitor model if known, else its screen technology. A later
session on the same phone and a matching screen uses it instead of the phone's
own, less accurate, color data.

Files live in <data folder>/profiles/, one small JSON file per phone and screen.
"""

import hashlib
import json
from pathlib import Path

from . import analyze, privacy

SCREEN_TECH_LABELS = {
    "woled": "WOLED", "qdoled": "QD-OLED", "oled_rgb": "OLED", "ips_ksf": "wide-gamut IPS (KSF)",
    "ips_wled": "IPS (white LED)", "va_qd": "VA quantum dot", "va_wled": "VA (white LED)",
    "mini_led_qd": "mini-LED quantum dot", "unknown": "this kind of screen",
}


def monitor_id(monitor):
    """Stable id of a monitor model from its EDID, e.g. "AUS:4242", or None if unknown."""
    if monitor and monitor.get("manufacturer") and monitor.get("product_code") is not None:
        return f"{monitor['manufacturer']}:{monitor['product_code']}"
    return None


def screen_of(monitor):
    """(monitor id, screen tech) of a cleaned monitor dict."""
    return monitor_id(monitor), (monitor or {}).get("screen_tech") or "unknown"


def _file(folder, phone, tech, mon):
    text = json.dumps([phone, tech, mon], sort_keys=True)
    return Path(folder) / f"{hashlib.sha256(text.encode()).hexdigest()[:24]}.json"


def save(folder, phone, tech, mon, k, label=None):
    """Save a correction for this phone and screen. Replaces an older one for the same pair."""
    if not phone or not analyze.valid_correction(k):
        return False
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    entry = {"phone": phone, "tech": tech, "monitor": mon, "k": [[float(v) for v in row] for row in k],
             "label": label or privacy.phone_label(phone)}
    path = _file(folder, phone, tech, mon)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    tmp.replace(path)
    return True


def find(folder, phone, tech, mon):
    """The saved correction that fits this phone and screen, as analyze_session() takes it, or None.

    The exact monitor model wins over its screen technology. A correction
    learned on a different technology is never used.
    """
    if not phone or not Path(folder).is_dir():
        return None
    candidates = []
    if mon:
        candidates.append(_file(folder, phone, tech, mon))
    candidates.append(_file(folder, phone, tech, None))
    for path in candidates:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if entry.get("phone") == phone and analyze.valid_correction(entry.get("k")):
            return {"k": entry["k"], "source": "local", "contributors": None, "spreadK": None,
                    "label": f"your saved correction for {privacy.phone_label(phone)} on {SCREEN_TECH_LABELS.get(tech, tech)}"}
    return None


def save_for_both(folder, phone, mon_id, tech, k):
    """Save under the exact monitor (if known) and under its screen technology."""
    ok = save(folder, phone, tech, None, k)
    if mon_id:
        ok = save(folder, phone, tech, mon_id, k) or ok
    return ok
