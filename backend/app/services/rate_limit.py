"""Fixed-window rate limiting in Redis.

One counter per (scope, identifier, window). INCR and EXPIRE run in a single
Lua script, so the counter can never be left without a TTL (which would lock
the caller out forever) even if the process dies between the two commands.

Fixed windows allow up to 2x the limit across a window boundary; that is an
accepted trade-off for auth endpoints, where the goal is to make brute force
impractical, not to meter precisely.
"""

import hashlib
from dataclasses import dataclass

from redis.asyncio import Redis

_HIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
    ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


@dataclass(frozen=True)
class RateLimitRule:
    limit: int
    window_seconds: int

    @classmethod
    def parse(cls, spec: str) -> "RateLimitRule":
        """Parse "<limit>/<window seconds>" (format validated in Settings)."""
        limit, window = spec.split("/")
        return cls(limit=int(limit), window_seconds=int(window))


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


def _key(scope: str, identifier: str) -> str:
    # Hash identifiers so raw emails/IPs never appear in Redis keys.
    digest = hashlib.sha256(identifier.encode()).hexdigest()[:32]
    return f"ratelimit:{scope}:{digest}"


async def hit(redis: Redis, scope: str, identifier: str, rule: RateLimitRule) -> RateLimitResult:
    """Count one request and report whether it is within the limit."""
    count, ttl = await redis.eval(  # type: ignore[misc]
        _HIT_SCRIPT, 1, _key(scope, identifier), str(rule.window_seconds)
    )
    count, ttl = int(count), int(ttl)
    return RateLimitResult(
        allowed=count <= rule.limit,
        remaining=max(rule.limit - count, 0),
        retry_after_seconds=max(ttl, 1),
    )


async def peek(redis: Redis, scope: str, identifier: str) -> tuple[int, int]:
    """Current (count, seconds until reset) for a counter, without counting."""
    key = _key(scope, identifier)
    count, ttl = await redis.get(key), await redis.ttl(key)
    return int(count or 0), max(int(ttl), 0)
