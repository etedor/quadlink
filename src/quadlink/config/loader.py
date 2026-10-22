"""Configuration loading with caching fallback."""

import asyncio
from pathlib import Path
from typing import Any

import structlog
from ruamel.yaml import YAML

from .models import Config

logger = structlog.get_logger()

# top-level keys Config does not define
_REMOVED_KEYS = ("skip_hosted", "hosted_offset", "logging")
# webhook.* keys Webhook does not define
_REMOVED_WEBHOOK_KEYS = ("timeout",)
# priorities.*.*.* (stream group) keys StreamGroup does not define
_REMOVED_GROUP_KEYS = ("limit",)


def strip_removed_keys(data: dict[str, Any]) -> None:
    """Warn about and drop each removed config key, in place, so Config keeps rejecting typos.

    Config itself has no `extra="ignore"`: a stray top-level key (a typo like
    stabilty_bonus) must still fail loudly. Only the specific keys this version
    stopped reading are let through, and only after being named in a warning.
    """
    for key in _REMOVED_KEYS:
        if key in data:
            logger.warning("config key has no effect and is ignored", key=key)
            del data[key]

    webhook = data.get("webhook")
    if isinstance(webhook, dict):
        for key in _REMOVED_WEBHOOK_KEYS:
            if key in webhook:
                logger.warning("config key has no effect and is ignored", key=f"webhook.{key}")
                del webhook[key]

    priorities = data.get("priorities")
    if isinstance(priorities, dict):
        for groups in priorities.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for key in _REMOVED_GROUP_KEYS:
                    if key in group:
                        logger.warning(
                            "config key has no effect and is ignored",
                            key=f"priorities.*.{key}",
                        )
                        del group[key]


class ConfigNotFoundError(Exception):
    """Raised when no configuration file is found in any search path."""


class ConfigLoader:
    """Loads configuration from YAML files with caching fallback.

    Searches multiple paths for config files and caches the last successful
    load to provide resilience against transient file access errors.

    Attributes:
        SEARCH_PATHS: Ordered list of config file locations to try.
    """

    SEARCH_PATHS = [
        "/app/config.yaml",
        "./config.yaml",
        "~/.quadlink/config.yaml",
        "/etc/quadlink/config.yaml",
    ]

    def __init__(self, explicit_path: str | None = None) -> None:
        """Initialize config loader with empty cache.

        Args:
            explicit_path: If provided, only load from this path (skip search).
        """
        self._cached_config: Config | None = None
        self._lock = asyncio.Lock()
        self._explicit_path = explicit_path

    async def load_or_cache(self) -> Config:
        """Load configuration from file or use cached version on failure.

        Returns:
            Loaded or cached Config object.

        Raises:
            ConfigNotFoundError: If no config file found and no cache exists.
        """
        async with self._lock:
            try:
                config = await self._load_from_file()
                self._cached_config = config
                return config
            except Exception as e:
                if self._cached_config is not None:
                    logger.warning(
                        "config load failed, using cached version",
                        error=str(e),
                    )
                    return self._cached_config
                raise

    async def _load_from_file(self) -> Config:
        """Load configuration from first available YAML file.

        Returns:
            Parsed Config object.

        Raises:
            ConfigNotFoundError: If no config file found in search paths.
        """
        yaml = YAML(typ="safe")

        # if explicit path provided, only try that
        search_paths = [self._explicit_path] if self._explicit_path else self.SEARCH_PATHS

        for path_str in search_paths:
            path = Path(path_str).expanduser().resolve()
            if path.exists():
                logger.debug("config loaded", path=str(path))
                with open(path) as f:
                    data = yaml.load(f)

                strip_removed_keys(data)
                config = Config(**data)

                if "stability_bonus" in data and "diversity_bonus" in data:
                    original_stability = data["stability_bonus"]
                    if config.stability_bonus != original_stability:
                        logger.warning(
                            "adjusted stability_bonus to prevent oscillation",
                            original=original_stability,
                            adjusted=config.stability_bonus,
                            reason=f"must be >= diversity_bonus ({config.diversity_bonus}) + 1",
                        )

                return config

        if self._explicit_path:
            raise ConfigNotFoundError(f"Config file not found: {self._explicit_path}")
        else:
            raise ConfigNotFoundError(
                f"No config file found. Searched: {', '.join(self.SEARCH_PATHS)}"
            )

    def _find_config_file(self) -> Path | None:
        """Find first available config file.

        Returns:
            Path to config file if found, None otherwise.
        """
        for path_str in self.SEARCH_PATHS:
            path = Path(path_str).expanduser().resolve()
            if path.exists():
                return path
        return None
