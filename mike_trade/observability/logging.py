"""
Structured JSON logging for mike_trade using Python standard library.
"""

import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TextIO

SENSITIVE_KEY_PATTERN = re.compile(
    r"(api_key|secret|token|password|authorization|apca-api-key-id|apca-api-secret-key)",
    re.IGNORECASE,
)

STANDARD_RECORD_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


def _redact_value(key: str, val: Any) -> Any:
    if SENSITIVE_KEY_PATTERN.search(key):
        return "***REDACTED***"
    if isinstance(val, Mapping):
        return {
            str(nested_key): _redact_value(str(nested_key), nested_value)
            for nested_key, nested_value in val.items()
        }
    if isinstance(val, (list, tuple)):
        return [_redact_value(key, item) for item in val]
    return val


def _default_serializer(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=UTC)
        else:
            obj = obj.astimezone(UTC)
        return obj.isoformat()
    return str(obj)


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created, tz=UTC)
        log_entry: dict[str, Any] = {
            "timestamp": dt.isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, val in record.__dict__.items():
            if key not in STANDARD_RECORD_ATTRS and key not in log_entry:
                log_entry[key] = _redact_value(key, val)

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=_default_serializer)


_HANDLER_ATTACHED: logging.Handler | None = None


def configure_logging(
    level: str = "INFO", *, json_output: bool = True, stream: TextIO | None = None
) -> None:
    """Configures application logging idempotently."""
    global _HANDLER_ATTACHED

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if _HANDLER_ATTACHED is not None and _HANDLER_ATTACHED in root_logger.handlers:
        root_logger.removeHandler(_HANDLER_ATTACHED)

    handler = logging.StreamHandler(stream)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    root_logger.addHandler(handler)
    _HANDLER_ATTACHED = handler


def get_logger(name: str) -> logging.Logger:
    """Returns a logger for the given name."""
    return logging.getLogger(name)
