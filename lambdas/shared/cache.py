"""Redis client accessor.

The client is created at module level so the connection pool is reused across
warm Lambda invocations. redis.Redis() is lazy — no socket is opened until the
first command is issued.

In production: REDIS_HOST points to the ElastiCache primary endpoint (injected
by SAM as an env var from the CloudFormation output).
In local dev / tests: point REDIS_HOST to localhost (Docker) or use fakeredis
to patch the module-level client.
"""
import os

import redis as _redis

_client: _redis.Redis = _redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)


def get_client() -> _redis.Redis:
    """Return the shared Redis client."""
    return _client
