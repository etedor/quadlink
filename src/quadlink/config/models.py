"""Configuration models using Pydantic."""

from pathlib import Path
from typing import Literal

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
    filters: Filters = Field(default_factory=Filters)


class StreamGroup(BaseModel):
    """Group of stream URLs with associated rulesets.

    Attributes:
        urls: List of Twitch usernames or URLs.
        rulesets: Names of rulesets to apply to these streams.
    """

    urls: list[str]
    rulesets: list[str] = Field(default_factory=list)


class Webhook(BaseModel):
    """Webhook notification configuration.

    Attributes:
        enabled: Whether webhook notifications are active.
        url: Webhook endpoint URL.
    """

    enabled: bool = False
    url: str = ""


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
        webhook: Webhook notification settings.
        proxy_playlist: Twitch playlist proxy URL for ad-free streams.
        low_latency: Enable low-latency streaming mode.
        lookup_timeout: Seconds before one channel lookup counts as failed.
        relay_segments: Serve segments from the relay and convert audio to 48 kHz.
    """

    model_config = SettingsConfigDict(
        env_prefix="QL_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    credentials: Credentials | None = None
    rulesets: list[Ruleset] = Field(default_factory=list)
    priorities: dict[int, list[StreamGroup]]

    diversity_bonus: int = 25
    stability_bonus: int = 30
    category_continuity_bonus: int = 10

    webhook: Webhook = Field(default_factory=Webhook)

    proxy_playlist: str = "https://eu.luminous.dev"
    low_latency: bool = True
    # real lookups take 0.5-1.7 s (bench/README.md); a channel that times out
    # gets 3x this on its next lookup
    lookup_timeout: float = Field(default=10.0, gt=0)

    # serve segments from duke (fetched once for all clients) with audio re-encoded
    # to 48 kHz; false sends clients straight to Twitch's CDN
    relay_segments: bool = True

    # where quadlink publishes the quad:
    #   both   - local relay + push to quadstream.tv (default, no regression)
    #   local  - local relay only; no quadstream login/push (credentials optional)
    #   remote - push to quadstream.tv only; local relay not served
    mode: Literal["both", "local", "remote"] = "both"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, v: object) -> object:
        """Accept case-insensitive mode values."""
        return v.strip().lower() if isinstance(v, str) else v

    @model_validator(mode="after")
    def validate_credentials_for_mode(self) -> "Config":
        """Require quadstream credentials unless running local-only.

        Returns:
            Validated Config instance.

        Raises:
            ValueError: If credentials are missing while quadstream is in use.
        """
        if self.mode != "local" and self.credentials is None:
            raise ValueError("credentials are required unless mode is 'local'")
        return self

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
