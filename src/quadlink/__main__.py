"""Entry point for quadlink daemon."""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from quadlink.daemon import run_daemon

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


def make_level_filter(min_level: str) -> structlog.types.Processor:
    """Filter log messages below min_level.

    Args:
        min_level: Minimum log level to pass through (debug, info, warn, error).

    Returns:
        Processor function for structlog pipeline.
    """
    min_level_num = LOG_LEVELS.get(min_level.lower(), 20)

    def level_filter(
        logger: structlog.types.WrappedLogger,
        method_name: str,
        event_dict: EventDict,
    ) -> ProcessorReturn:
        level_num = LOG_LEVELS.get(method_name, 20)
        if level_num < min_level_num:
            raise structlog.DropEvent
        return event_dict

    return level_filter


def setup_logging(level: str = "info") -> None:
    """Configure structlog for the application.

    Uses console output for TTY, JSON for non-TTY (e.g., container logs).

    Args:
        level: Minimum log level to output.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            make_level_filter(level),
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


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(description="QuadLink stream curation daemon")
    parser.add_argument(
        "--config",
        type=str,
        help="Path to config.yaml file (default: auto-discover from standard paths)",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Run once and exit (don't loop)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between quad updates (default: 30)",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warn", "error"],
        default=os.environ.get("QL_LOG_LEVEL", "info").lower(),
        help="Log level (default: info, or QL_LOG_LEVEL env var)",
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point for quadlink daemon."""
    args = parse_args()
    setup_logging(args.log_level)

    # health server disabled by default (enable for Docker)
    enable_health_server = os.environ.get("QL_ENABLE_HEALTH_SERVER", "false").lower() in (
        "true",
        "1",
        "yes",
    )

    logger = structlog.get_logger()
    mode = "one-shot" if args.one_shot else "daemon"
    logger.info("starting quadlink", mode=mode, interval=args.interval)

    try:
        asyncio.run(
            run_daemon(
                one_shot=args.one_shot,
                interval=args.interval,
                enable_health_server=enable_health_server,
                config_path=args.config,
            )
        )
    except KeyboardInterrupt:
        logger.info("daemon interrupted by user")
    except Exception as e:
        logger.error("daemon failed", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
