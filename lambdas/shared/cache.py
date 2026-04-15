import os
import redis as _redis

# Initialized outside handler — connection reused across warm Lambda invocations
_client = _redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)


def get_client() -> _redis.Redis:
    return _client
