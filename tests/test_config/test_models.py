"""Tests for configuration models."""

import pytest
from pydantic import ValidationError

from quadlink.config.models import (
    Config,
    Credentials,
    Filters,
    Logging,
    Ruleset,
    StreamGroup,
    Webhook,
)


def test_credentials_valid():
    """Test valid credentials."""
    creds = Credentials(username="test", secret="secret123")
    assert creds.username == "test"
    assert creds.secret == "secret123"


def test_credentials_missing_secret():
    """Test credentials require both username and secret."""
    with pytest.raises(ValidationError, match="Both username and secret required"):
        Credentials(username="test")


def test_credentials_from_file(tmp_path):
    """Test reading credentials from file (username:password format)."""
    creds_file = tmp_path / "creds.txt"
    creds_file.write_text("testuser:my-secret-value\n")

    creds = Credentials(file=str(creds_file))
    assert creds.username == "testuser"
    assert creds.secret == "my-secret-value"
    assert creds.file == str(creds_file)


def test_credentials_file_password_only(tmp_path):
    """Test reading password-only file (uses username from config)."""
    creds_file = tmp_path / "secret.txt"
    creds_file.write_text("my-secret-value\n")

    creds = Credentials(username="test", file=str(creds_file))
    assert creds.username == "test"
    assert creds.secret == "my-secret-value"


def test_credentials_file_not_found():
    """Test error when credentials file doesn't exist."""
    with pytest.raises(ValidationError, match="Failed to read credentials from"):
        Credentials(file="/nonexistent/file.txt")


def test_credentials_prefer_file_over_inline():
    """Test that file overrides inline credentials if both provided."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("fileuser:file-secret")
        creds_file = f.name

    try:
        creds = Credentials(username="test", secret="inline-secret", file=creds_file)
        assert creds.username == "fileuser"
        assert creds.secret == "file-secret"
    finally:
        import os

        os.unlink(creds_file)


def test_filters_defaults():
    """Test filters default to empty lists."""
    filters = Filters()
    assert filters.allow_categories == []
    assert filters.allow_titles == []
    assert filters.block_categories == []
    assert filters.block_titles == []


def test_filters_with_values():
    """Test filters with regex patterns."""
    filters = Filters(
        allow_categories=["^Destiny 2$"],
        block_titles=["(?i).*raid.*"],
    )
    assert len(filters.allow_categories) == 1
    assert len(filters.block_titles) == 1


def test_ruleset():
    """Test ruleset with filters."""
    ruleset = Ruleset(
        name="test",
        filters=Filters(block_categories=["^Minecraft$"]),
    )
    assert ruleset.name == "test"
    assert len(ruleset.filters.block_categories) == 1


def test_stream_group():
    """Test stream group."""
    group = StreamGroup(urls=["streamer1", "streamer2"], rulesets=["global"])
    assert len(group.urls) == 2
    assert group.rulesets == ["global"]
    assert group.limit is None


def test_webhook_defaults():
    """Test webhook defaults to disabled."""
    webhook = Webhook()
    assert webhook.enabled is False
    assert webhook.url == ""
    assert webhook.timeout == 10


def test_logging_defaults():
    """Test logging defaults."""
    logging = Logging()
    assert logging.level == "info"
    assert logging.format == "json"


def test_logging_validates_level():
    """Test logging level validation."""
    # valid levels
    for level in ["debug", "info", "warn", "warning", "error", "critical"]:
        log = Logging(level=level)
        assert log.level == level.lower()

    # invalid level
    with pytest.raises(ValidationError, match="Invalid log level"):
        Logging(level="invalid")


def test_logging_validates_format():
    """Test logging format validation."""
    # valid formats
    for fmt in ["json", "text", "console"]:
        log = Logging(format=fmt)
        assert log.format == fmt.lower()

    # invalid format
    with pytest.raises(ValidationError, match="Invalid log format"):
        Logging(format="invalid")


def test_config_minimal():
    """Test minimal valid config."""
    config_data = {
        "credentials": {"username": "user", "secret": "pass"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
    }
    config = Config(**config_data)
    assert config.credentials.username == "user"
    assert 999 in config.priorities
    assert config.diversity_bonus == 25  # default
    assert config.stability_bonus == 30  # default


def test_config_bonus_auto_adjustment():
    """Test that stability_bonus is auto-adjusted if too low."""
    config_data = {
        "credentials": {"username": "user", "secret": "pass"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
        "diversity_bonus": 25,
        "stability_bonus": 20,  # too low! should be >= 26
    }
    config = Config(**config_data)

    # should be auto-adjusted to diversity_bonus + 1
    assert config.stability_bonus == 26
    assert config.diversity_bonus == 25


def test_config_bonus_valid():
    """Test that valid bonuses are not adjusted."""
    config_data = {
        "credentials": {"username": "user", "secret": "pass"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
        "diversity_bonus": 25,
        "stability_bonus": 30,  # valid: 30 >= 25 + 1
    }
    config = Config(**config_data)

    # should not be adjusted
    assert config.stability_bonus == 30
    assert config.diversity_bonus == 25


def test_config_get_ruleset():
    """Test getting ruleset by name."""
    config_data = {
        "credentials": {"username": "user", "secret": "pass"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
        "rulesets": [
            {"name": "global", "filters": {"block_categories": ["^Minecraft$"]}},
            {"name": "destiny", "filters": {"allow_categories": ["^Destiny 2$"]}},
        ],
    }
    config = Config(**config_data)

    # find existing ruleset
    global_ruleset = config.get_ruleset("global")
    assert global_ruleset is not None
    assert global_ruleset.name == "global"

    # ruleset not found
    missing = config.get_ruleset("nonexistent")
    assert missing is None


def test_config_full():
    """Test full configuration with all features."""
    config_data = {
        "credentials": {"username": "user", "secret": "pass"},
        "priorities": {
            999: [
                {"urls": ["streamer1"], "rulesets": ["global"]},
                {"urls": ["streamer2", "streamer3"]},
            ],
            100: [{"urls": ["streamer4"]}],
        },
        "rulesets": [
            {
                "name": "global",
                "filters": {
                    "block_categories": ["^Minecraft$", "^Just Chatting$"],
                    "block_titles": ["(?i).*!adfest.*"],
                },
            }
        ],
        "diversity_bonus": 30,
        "stability_bonus": 40,
        "skip_hosted": True,
        "hosted_offset": 50,
        "webhook": {"enabled": True, "url": "https://example.com", "timeout": 15},
        "logging": {"level": "debug", "format": "text"},
        "proxy_playlist": "https://custom-proxy.com",
        "low_latency": False,
    }
    config = Config(**config_data)

    # verify all fields
    assert config.credentials.username == "user"
    assert len(config.priorities) == 2
    assert len(config.priorities[999]) == 2
    assert len(config.rulesets) == 1
    assert config.diversity_bonus == 30
    assert config.stability_bonus == 40
    assert config.skip_hosted is True
    assert config.hosted_offset == 50
    assert config.webhook.enabled is True
    assert config.webhook.url == "https://example.com"
    assert config.logging.level == "debug"
    assert config.logging.format == "text"
    assert config.proxy_playlist == "https://custom-proxy.com"
    assert config.low_latency is False
