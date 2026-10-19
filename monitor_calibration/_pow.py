"""Proof of work shared by the app (which solves) and the API (which checks).

A solution is a counter such that SHA-256("nonce:counter") starts with
`difficulty` zero bits. Standard library only.
"""

import hashlib

# The app refuses harder challenges than this, so a bad server can't make it spin for minutes.
MAX_DIFFICULTY = 24


def leading_zero_bits(digest):
    n = int.from_bytes(digest, "big")
    return len(digest) * 8 - n.bit_length()


def work_ok(nonce, counter, difficulty):
    return leading_zero_bits(hashlib.sha256(f"{nonce}:{counter}".encode()).digest()) >= difficulty


def solve(challenge):
    """Find the counter. Takes about a second at the default difficulty."""
    nonce, difficulty = challenge["nonce"], challenge["difficulty"]
    if not isinstance(nonce, str) or not isinstance(difficulty, int) or not 1 <= difficulty <= MAX_DIFFICULTY:
        raise ValueError("bad challenge")
    counter = 0
    while not work_ok(nonce, counter, difficulty):
        counter += 1
    return counter
