"""Configuration models using Pydantic."""

from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Credentials(BaseModel):
    """QuadStream API credentials.

    Attributes:
        username: QuadStream account username (optional if provided in file).
        secret: QuadStream account secret/password (optional if provided in file).
        file: Path to file containing credentials (username:password or password only).
    """

    username: str | None = None
    secret: str | None = None
    file: str | None = None

    @model_validator(mode="after")
    def validate_credentials(self) -> "Credentials":
        """Ensure username and secret are provided.

        Returns:
            Validated Credentials instance.

        Raises:
            ValueError: If credentials incomplete or file unreadable.
        """
        if self.file:
            try:
                file_path = Path(self.file).expanduser()
                content = file_path.read_text().strip()

                if ":" in content:
                    # format: username:password
                    self.username, self.secret = content.split(":", 1)
                else:
                    # format: password only (username must be in config)
                    self.secret = content

            except Exception as e:
                raise ValueError(f"Failed to read credentials from {self.file}: {e}") from e

        if not self.username or not self.secret:
            raise ValueError("Both username and secret required (provide inline or via 'file')")

        return self


class Filters(BaseModel):
    """Regex filters for stream categories and titles.

    Attributes:
        allow_categories: Patterns that categories must match (allowlist).
        allow_titles: Patterns that titles must match (allowlist).
        block_categories: Patterns that reject matching categories (blocklist).
        block_titles: Patterns that reject matching titles (blocklist).
    """

    allow_categories: list[str] = Field(default_factory=list)
    allow_titles: list[str] = Field(default_factory=list)
    block_categories: list[str] = Field(default_factory=list)
    block_titles: list[str] = Field(default_factory=list)


class Ruleset(BaseModel):
    """Named collection of filters.

    Attributes:
        name: Unique identifier for this ruleset.
        filters: Filter patterns to apply.
    """

    name: str
    filters: Filters


class StreamGroup(BaseModel):
    """Group of stream URLs with associated rulesets.

    Attributes:
        urls: List of Twitch usernames or URLs.
        rulesets: Names of rulesets to apply to these streams.
        limit: Maximum streams to select from this group.
    """

    urls: list[str]
    rulesets: list[str] = Field(default_factory=list)
    limit: int | None = None


class Webhook(BaseModel):
    """Webhook notification configuration.

    Attributes:
        enabled: Whether webhook notifications are active.
        url: Webhook endpoint URL.
        timeout: Request timeout in seconds.
    """

    enabled: bool = False
    url: str = ""
    timeout: int = 10


class Logging(BaseModel):
    """Logging configuration.

    Attributes:
        level: Log level (debug, info, warn, error, critical).
        format: Output format (json, text, console).
    """

    level: str = "info"
    format: str = "json"

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        """Validate and normalize log level.

        Args:
            v: Input log level string.

        Returns:
            Lowercase validated log level.

        Raises:
            ValueError: If level is not recognized.
        """
        valid_levels = ["debug", "info", "warn", "warning", "error", "critical"]
        if v.lower() not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return v.lower()

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        """Validate and normalize log format.

        Args:
            v: Input log format string.

        Returns:
            Lowercase validated log format.

        Raises:
            ValueError: If format is not recognized.
        """
        valid_formats = ["json", "text", "console"]
        if v.lower() not in valid_formats:
            raise ValueError(f"Invalid log format: {v}. Must be one of {valid_formats}")
        return v.lower()


class Config(BaseSettings):
    """Main application configuration.

    Supports environment variable overrides with QL_ prefix.

    Attributes:
        credentials: QuadStream API credentials.
        rulesets: Named filter collections.
        priorities: Priority levels mapped to stream groups.
        diversity_bonus: Points added for new categories in quad.
        stability_bonus: Points added for streams already in quad.
        category_continuity_bonus: Points for replacing departed category.
        skip_hosted: Whether to skip hosted/raided streams.
        hosted_offset: Priority reduction for hosted streams (if not skipped).
        webhook: Webhook notification settings.
        logging: Logging configuration.
        proxy_playlist: Twitch playlist proxy URL for ad-free streams.
        low_latency: Enable low-latency streaming mode.
    """

    model_config = SettingsConfigDict(
        env_prefix="QL_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    credentials: Credentials
    rulesets: list[Ruleset] = Field(default_factory=list)
    priorities: dict[int, list[StreamGroup]]

    diversity_bonus: int = 25
    stability_bonus: int = 30
    category_continuity_bonus: int = 10
    skip_hosted: bool = True
    hosted_offset: int = 50

    webhook: Webhook = Field(default_factory=Webhook)
    logging: Logging = Field(default_factory=Logging)

    proxy_playlist: str = "https://eu.luminous.dev"
    low_latency: bool = True

    @model_validator(mode="after")
    def validate_bonuses(self) -> "Config":
        """Ensure stability bonus >= diversity bonus + 1 to prevent oscillation.

        Auto-adjusts stability_bonus if too low rather than failing validation.

        Returns:
            Validated Config instance.
        """
        if self.stability_bonus < self.diversity_bonus + 1:
            self.stability_bonus = self.diversity_bonus + 1

        return self

    def get_ruleset(self, name: str) -> Ruleset | None:
        """Get a ruleset by name.

        Args:
            name: Ruleset name to look up.

        Returns:
            Ruleset if found, None otherwise.
        """
        for ruleset in self.rulesets:
            if ruleset.name == name:
                return ruleset
        return None
