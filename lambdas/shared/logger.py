"""Structured JSON logging for all Lambda functions.

Every log line includes at minimum: level, event, env, traceId (if provided).
Output goes to stdout; CloudWatch Logs picks it up automatically.
"""
import json
import logging
import os

_logger = logging.getLogger()
_logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_ENV = os.environ.get("ENV", "unknown")


def _emit(level: str, event: str, **kwargs) -> None:
    record: dict = {
        "level": level,
        "event": event,
        "env": _ENV,
        **kwargs,
    }
    getattr(_logger, level.lower())(json.dumps(record))


def info(event: str, **kwargs) -> None:
    _emit("INFO", event, **kwargs)


def warn(event: str, **kwargs) -> None:
    _emit("WARNING", event, **kwargs)


def error(event: str, **kwargs) -> None:
    _emit("ERROR", event, **kwargs)


def debug(event: str, **kwargs) -> None:
    _emit("DEBUG", event, **kwargs)
