"""Admin tool: look at what the shared database is holding back, and decide.

    python scripts/review.py list
    python scripts/review.py approve SUBMISSION_ID     accept a quarantined submission
    python scripts/review.py reject SUBMISSION_ID
    python scripts/review.py unfreeze PHONE_ID TECH [MONITOR_ID]
    python scripts/review.py block-phone PHONE_ID      keep a phone model out of the database
    python scripts/review.py block-monitor MONITOR_ID
    python scripts/review.py aggregate                 run the hourly job now

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment. Run it
from your own machine; the service-role key bypasses every protection, so it
never goes anywhere else.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.v1._lib import aggregate  # noqa: E402
from api.v1._lib.config import Context  # noqa: E402
from api.v1._lib.supabase_store import SupabaseStore  # noqa: E402


def make_context(store=None):
    if store is None:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY first.")
        store = SupabaseStore(url, key)
    return Context(config=None, store=store)


def show(ctx, out=print):
    store = ctx.store
    quarantined = store.list_submissions(status="quarantined")
    out(f"Quarantined submissions: {len(quarantined)}")
    for s in quarantined:
        out(f"  #{s['id']}  phone {s['phone_id']}  {s['reference_screen_tech']}  received {s['received_on']}")
    frozen = store.list_corrections(frozen=True)
    out(f"Frozen keys: {len(frozen)}")
    for c in frozen:
        out(f"  phone {c['phone_id']}  monitor {c.get('monitor_id')}  {c['screen_tech']}  (serving nothing until unfrozen)")
    out(f"Pending submissions: {len(store.list_submissions(status='pending'))}")
    for label, rows in (("Phones", store.list_phones()), ("Monitors", store.list_monitors())):
        by = {}
        for r in rows:
            by.setdefault(r["status"], []).append(r)
        out(f"{label}: " + (", ".join(f"{len(v)} {k}" for k, v in sorted(by.items())) or "none"))
    for p in store.list_phones("pending")[:20]:
        out(f"  pending phone {p['id']}: {p['unique_camera_model']} ({p['app_family']}, {p['raw_type']})")
    for m in store.list_monitors("pending")[:20]:
        out(f"  pending monitor {m['id']}: {m['model_name'] or '(unnamed)'} {m['edid_manufacturer'] or ''}:{m['edid_product_code']}")


def decide(ctx, sid, status):
    ctx.store.update_submission(sid, {"status": status})
    sub = next((s for s in ctx.store.list_submissions() if s["id"] == sid), None)
    if sub:
        aggregate.recompute_phone(ctx, sub["phone_id"], tripwire=False)


def unfreeze(ctx, phone_id, tech, monitor_id=None):
    row = ctx.store.get_correction(phone_id, monitor_id, tech)
    if row is None:
        raise SystemExit("No such key.")
    ctx.store.delete_correction(phone_id, monitor_id, tech)
    aggregate.recompute_phone(ctx, phone_id, tripwire=False)


def main(argv=None, ctx=None, out=print):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("aggregate")
    for name in ("approve", "reject", "block-phone", "block-monitor"):
        sub.add_parser(name).add_argument("id", type=int)
    un = sub.add_parser("unfreeze")
    un.add_argument("phone_id", type=int)
    un.add_argument("tech")
    un.add_argument("monitor_id", type=int, nargs="?")
    args = ap.parse_args(argv)
    ctx = ctx or make_context()
    if args.cmd == "list":
        show(ctx, out)
    elif args.cmd == "approve":
        decide(ctx, args.id, "accepted")
    elif args.cmd == "reject":
        decide(ctx, args.id, "rejected")
    elif args.cmd == "unfreeze":
        unfreeze(ctx, args.phone_id, args.tech, args.monitor_id)
    elif args.cmd == "block-phone":
        ctx.store.update_phone(args.id, {"status": "blocked"})
    elif args.cmd == "block-monitor":
        ctx.store.update_monitor(args.id, {"status": "blocked"})
    elif args.cmd == "aggregate":
        out(aggregate.run(ctx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
