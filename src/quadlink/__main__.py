"""Entry point for quadlink daemon."""

import argparse
import asyncio
import os
import sys

import structlog

from quadlink.daemon import run_daemon
from quadlink.logconfig import setup_logging


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
        default=None,
        help="Log level; overrides the config's log_level (default: the config value, or info)",
    )
    parser.add_argument(
        "--webui",
        action="store_true",
        help="Enable web UI for config editing",
    )
    parser.add_argument(
        "--webui-host",
        type=str,
        default="0.0.0.0",
        help="Host address for web UI (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--webui-port",
        type=int,
        default=8081,
        help="Port for web UI (default: 8081)",
    )
    parser.add_argument(
        "--webui-allowed-host",
        action="append",
        default=[],
        metavar="HOST",
        dest="webui_allowed_hosts",
        help=(
            "Host name the web UI page and API answer to, in addition to localhost, "
            "127.0.0.1 and ::1 (repeatable)"
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point for quadlink daemon."""
    args = parse_args()
    setup_logging(args.log_level)

    # health server disabled by default (enable for a container's health check)
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
                enable_webui=args.webui,
                webui_host=args.webui_host,
                webui_port=args.webui_port,
                webui_allowed_hosts=args.webui_allowed_hosts,
            )
        )
    except KeyboardInterrupt:
        logger.info("daemon interrupted by user")
    except Exception as e:
        logger.error("daemon failed", error=str(e), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
