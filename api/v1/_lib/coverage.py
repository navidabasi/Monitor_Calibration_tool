"""Public coverage: how much the shared database knows, as counts only (no names, no per-person data)."""

from collections import Counter

from . import aggregate, recipes, refprofiles


def build(store):
    served = [c for c in store.list_corrections(frozen=False) if c["contributors"] >= aggregate.MIN_CONTRIBUTORS]
    by_tech = Counter(c["screen_tech"] for c in served if c["monitor_id"] is None)
    return {
        "phones_verified": len(store.list_phones("verified")),
        "monitors_verified": len(store.list_monitors("verified")),
        "phone_corrections": {"phones_covered": len({c["phone_id"] for c in served}), "by_screen_tech": dict(sorted(by_tech.items()))},
        "suggested_settings": {"monitor_modes": sum(1 for k in store.settings_keys() if recipes.compute(store, *k))},
        "reference_profiles": {"monitor_modes": sum(1 for k in store.reference_keys() if refprofiles.compute(store, *k))},
    }
