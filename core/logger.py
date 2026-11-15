import json
import logging
import sys
from datetime import UTC, datetime

from core.observability import correlation_id, update_id


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter with safe correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id(),
            "update_id": update_id(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logging() -> None:
    """Configure application loggers without replacing Uvicorn's own handlers."""
    app_logger = logging.getLogger("core")
    if any(getattr(handler, "_structured_json", False) for handler in app_logger.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._structured_json = True  # type: ignore[attr-defined]
    app_logger.handlers = [handler]
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
