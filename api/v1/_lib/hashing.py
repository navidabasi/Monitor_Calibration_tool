"""Hashes of network addresses. The address itself is never stored or logged.

Two kinds, both HMACs keyed by a random salt kept in the database and deleted
when its period is over, so an old hash can't be reversed or linked to a later one:

  rate_key         per day, per endpoint: rate limiting. Rows are purged after 48 hours.
  contributor_hash per month, of the network prefix (/24 IPv4, /48 IPv6): counts independent
                   contributors. Nulled on the submission after 60 days.
"""

import hashlib
import hmac
import ipaddress
import time


def client_ip(headers):
    """The caller's address as Vercel reports it. Only ever used to build the hashes below."""
    ip = (headers.get("x-real-ip") or headers.get("x-forwarded-for", "").split(",")[0] or "").strip()
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return "0.0.0.0"


def network_prefix(ip):
    addr = ipaddress.ip_address(ip)
    bits = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))


def day_of(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def month_of(ts):
    return time.strftime("%Y-%m", time.gmtime(ts))


def hour_start(ts):
    return time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(ts))


def day_start(ts):
    return time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(ts))


def _mac(ctx, kind, period, message):
    salt = ctx.store.salt(kind, period)
    key = hmac.new(ctx.config.ip_secret, salt, hashlib.sha256).digest()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def rate_key(ctx, ip, bucket):
    return _mac(ctx, "day", day_of(ctx.now()), f"{bucket}|{ip}")


def contributor_hash(ctx, ip):
    return _mac(ctx, "month", month_of(ctx.now()), network_prefix(ip))
