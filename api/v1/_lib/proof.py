"""Proof of work: every submission costs the sender about a second of computing.

Stateless issue, stateful redeem:
  issue   {nonce, difficulty, expires, sig}; sig = HMAC(secret, nonce|difficulty|expires)
  redeem  the client finds a counter so that SHA-256("nonce:counter") starts with
          `difficulty` zero bits. The server checks the signature, the expiry
          and the work, and remembers the nonce so it can't be used twice.
"""

import hashlib
import hmac
import secrets

from monitor_calibration._pow import leading_zero_bits, solve, work_ok  # noqa: F401 - re-exported for callers and tests

from .config import CHALLENGE_LIFETIME


def _sig(secret, nonce, difficulty, expires):
    return hmac.new(secret, f"{nonce}|{difficulty}|{expires}".encode(), hashlib.sha256).hexdigest()


def issue(ctx):
    nonce = secrets.token_hex(16)
    difficulty = ctx.config.pow_difficulty
    expires = ctx.now() + CHALLENGE_LIFETIME
    return {"nonce": nonce, "difficulty": difficulty, "expires": expires, "sig": _sig(ctx.config.pow_secret, nonce, difficulty, expires)}


def redeem(ctx, solution):
    """True if the solution is genuine, fresh, solved and unused. Marks it used."""
    nonce, difficulty, expires = solution["nonce"], solution["difficulty"], solution["expires"]
    if not hmac.compare_digest(_sig(ctx.config.pow_secret, nonce, difficulty, expires), solution["sig"]):
        return False
    if expires < ctx.now() or expires > ctx.now() + CHALLENGE_LIFETIME + 60:
        return False
    if difficulty < ctx.config.pow_difficulty and ctx.config.pow_difficulty - difficulty > 4:
        # A challenge issued before the difficulty was raised is fine for a while; far easier ones are not.
        return False
    if not work_ok(nonce, solution["counter"], difficulty):
        return False
    return ctx.store.use_nonce(hashlib.sha256(nonce.encode()).hexdigest(), expires)
