from redis.asyncio import Redis

from app.core.config import get_settings

# One process-wide client; redis-py manages a connection pool internally and
# connects lazily, so importing this module never opens a connection.
redis_client: Redis = Redis.from_url(
    get_settings().redis_url,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)


async def get_redis() -> Redis:
    """FastAPI dependency returning the shared Redis client."""
    return redis_client
