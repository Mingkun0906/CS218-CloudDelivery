import json
import logging
import os

_logger = logging.getLogger()
_logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def log(level: str, event: str, **kwargs):
    """Emit a structured JSON log line."""
    record = {
        "level": level,
        "event": event,
        "env": os.environ.get("ENV", "unknown"),
        **kwargs,
    }
    getattr(_logger, level.lower())(json.dumps(record))


def info(event: str, **kwargs):
    log("INFO", event, **kwargs)


def warn(event: str, **kwargs):
    log("WARNING", event, **kwargs)


def error(event: str, **kwargs):
    log("ERROR", event, **kwargs)
