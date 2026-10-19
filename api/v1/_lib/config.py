"""Settings and the pieces the endpoints share (configuration, database, clock).

Everything secret comes from environment variables, which only exist on the
server. Tests build a Context by hand with a MemoryStore and a fixed clock.
"""

import os
import time
from dataclasses import dataclass, field
from typing import Callable

DEFAULT_POW_DIFFICULTY = 20  # leading zero bits: about 1-2 s of work on a laptop
MAX_BODY = 64 * 1024
CHALLENGE_LIFETIME = 600  # seconds


@dataclass
class Config:
    pow_secret: bytes
    ip_secret: bytes
    turnstile_secret: str = ""
    site_origin: str = ""
    cron_secret: str = ""
    pow_difficulty: int = DEFAULT_POW_DIFFICULTY
    max_body: int = MAX_BODY


def config_from_env(env=None):
    env = os.environ if env is None else env
    pow_secret, ip_secret = env.get("POW_SECRET", ""), env.get("IP_SALT_SECRET", "")
    if len(pow_secret) < 32 or len(ip_secret) < 32:
        raise RuntimeError("POW_SECRET and IP_SALT_SECRET must be set to at least 32 characters")
    return Config(
        pow_secret=pow_secret.encode(),
        ip_secret=ip_secret.encode(),
        turnstile_secret=env.get("TURNSTILE_SECRET", ""),
        site_origin=env.get("SITE_ORIGIN", "").rstrip("/"),
        cron_secret=env.get("CRON_SECRET", ""),
        pow_difficulty=int(env.get("POW_DIFFICULTY") or DEFAULT_POW_DIFFICULTY),
    )


def _post_json(url, data, timeout=5):
    """POST form data and return the JSON answer (used for Turnstile). Replaced in tests."""
    import json
    import urllib.parse
    import urllib.request

    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        return json.loads(resp.read(65536))


@dataclass
class Context:
    config: Config
    store: object
    clock: Callable[[], float] = time.time
    http_post: Callable = field(default=_post_json)

    def now(self):
        return int(self.clock())
