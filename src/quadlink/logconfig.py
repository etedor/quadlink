"""structlog setup with a minimum level that can change after startup."""

import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

EventDict = MutableMapping[str, Any]
ProcessorReturn = Mapping[str, Any] | str | bytes | bytearray | tuple[Any, ...]

LOG_LEVELS = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "warning": 30,
    "error": 40,
    "critical": 50,
}

_level_num = LOG_LEVELS["info"]
# set once a CLI --log-level pins the level, so a config log_level is then ignored
_pinned = False


def _level_filter(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> ProcessorReturn:
    """Drop events below the current minimum level."""
    if LOG_LEVELS.get(method_name, 20) < _level_num:
        raise structlog.DropEvent
    return event_dict


def set_level(level: str) -> None:
    """Set the minimum level the filter passes through."""
    global _level_num
    _level_num = LOG_LEVELS.get(level.lower(), LOG_LEVELS["info"])


def apply_config_level(level: str | None) -> None:
    """Apply a reloaded config's log_level, unless a CLI flag pinned the level."""
    if not _pinned:
        set_level(level if level is not None else "info")


def setup_logging(level: str | None = None) -> None:
    """Configure structlog for the application.

    Uses console output for TTY, JSON for non-TTY (e.g., container logs).

    Args:
        level: CLI --log-level value. Given, it pins the level so a config
            log_level cannot override it; omitted, the config decides (or "info").
    """
    global _pinned
    if level is not None:
        set_level(level)
        _pinned = True
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _level_filter,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            (
                structlog.dev.ConsoleRenderer()
                if sys.stderr.isatty()
                else structlog.processors.JSONRenderer()
            ),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
