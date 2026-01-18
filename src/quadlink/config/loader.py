"""Configuration loading with caching fallback."""

import asyncio
from pathlib import Path

import structlog
from ruamel.yaml import YAML

from .models import Config

logger = structlog.get_logger()


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
                logger.info("config loaded", path=str(path))
                with open(path) as f:
                    data = yaml.load(f)

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

    async def has_config(self) -> bool:
        """Check if configuration is available (loaded or cached).

        Returns:
            True if config file exists or cache is populated.
        """
        return self._cached_config is not None or self._find_config_file() is not None

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
