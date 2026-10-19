"""The admin review tool, against an in-memory database."""

import io
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from api.v1._lib import aggregate  # noqa: E402
import review  # noqa: E402
from test_api import Helpers, make_ctx  # noqa: E402


class Review(Helpers):
    def run_cli(self, ctx, *argv):
        lines = []
        review.main(list(argv), ctx=ctx, out=lambda *a: lines.append(" ".join(str(x) for x in a)))
        return "\n".join(lines)

    def test_quarantine_freeze_and_decisions(self):
        ctx = make_ctx()
        rng = np.random.default_rng(31)
        self.fill(ctx, 4, rng)
        aggregate.run(ctx)
        self.fill(ctx, 2, rng, start_ip=70, scale=(0.9, 1.0, 1.12))  # disagrees with the established value
        aggregate.run(ctx)
        text = self.run_cli(ctx, "list")
        self.assertIn("Quarantined submissions: 2", text)
        self.assertIn("Frozen keys: 0", text)
        quarantined = [s for s in ctx.store.submissions.values() if s["status"] == "quarantined"]

        # Approving counts it as a contributor; rejecting does not.
        self.run_cli(ctx, "approve", str(quarantined[0]["id"]))
        self.run_cli(ctx, "reject", str(quarantined[1]["id"]))
        self.assertEqual(ctx.store.submissions[quarantined[1]["id"]]["status"], "rejected")
        self.assertEqual(self.lookup(ctx)["contributors"], 5)

        # A flood freezes the key; unfreezing rebuilds it from what was accepted.
        self.fill(ctx, 22, rng, start_ip=120, days=0)
        aggregate.run(ctx)
        self.assertIn("Frozen keys: 1", self.run_cli(ctx, "list"))
        self.assertEqual(self.lookup(ctx), {"found": False})
        frozen = ctx.store.list_corrections(frozen=True)[0]
        self.run_cli(ctx, "unfreeze", str(frozen["phone_id"]), frozen["screen_tech"])
        self.assertTrue(self.lookup(ctx)["found"])

        # Blocking a phone keeps it out.
        phone_id = frozen["phone_id"]
        self.run_cli(ctx, "block-phone", str(phone_id))
        self.assertEqual(ctx.store.phones[phone_id]["status"], "blocked")
        (status, *_), _ = self.submit(ctx, ip="10.200.0.1")
        self.assertEqual(status, 422)

    def test_needs_credentials(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            review.make_context()


if __name__ == "__main__":
    unittest.main()
