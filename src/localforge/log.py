"""Logging configuration for LocalForge.

Supports two output formats:
  - "human" (default): readable colored output for development
  - "json": structured JSON lines for production / log aggregation

Usage:
    from localforge.log import setup_logging

    setup_logging()                    # human-readable to stderr
    setup_logging(fmt="json")          # JSON lines to stderr
    setup_logging(level="DEBUG")       # verbose

All LocalForge modules should use:
    import logging
    log = logging.getLogger("localforge")

or a sub-logger:
    log = logging.getLogger("localforge.client")
"""

import json
import logging
import sys
from typing import Any


# ---------------------------------------------------------------------------
# JSON formatter — one JSON object per log line
# ---------------------------------------------------------------------------
class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        # Include any extra fields attached to the record
        for key in ("request_id", "tool", "model", "backend", "duration_ms"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry, default=str)


# ---------------------------------------------------------------------------
# Human-readable formatter
# ---------------------------------------------------------------------------
HUMAN_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def setup_logging(
    fmt: str = "human",
    level: str = "INFO",
    stream: Any = None,
) -> None:
    """Configure the root 'localforge' logger.

    Args:
        fmt: "human" for readable output, "json" for structured JSON lines.
        level: Log level name (DEBUG, INFO, WARNING, ERROR).
        stream: Output stream (default: sys.stderr).
    """
    if stream is None:
        stream = sys.stderr

    log_level = getattr(logging, level.upper(), logging.INFO)

    # Configure the root logger so ALL modules get the handler
    # (agents, gpu_pool, etc. use non-"localforge" logger names)
    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove existing handlers to avoid duplicates on re-init
    root.handlers.clear()

    handler = logging.StreamHandler(stream)
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(HUMAN_FMT))

    root.addHandler(handler)

    # Also ensure the localforge logger is at the right level
    logging.getLogger("localforge").setLevel(log_level)

    # Quiet down noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
