"""Tests for configuration models."""

import pytest
import structlog.testing
from pydantic import ValidationError

from quadlink.config.models import (
    Config,
    Credentials,
    Filters,
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


def test_webhook_defaults():
    """Test webhook defaults to disabled."""
    webhook = Webhook()
    assert webhook.enabled is False
    assert webhook.url == ""


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
    assert config.lookup_timeout == 10


@pytest.mark.parametrize("value", [0, -1, 121])
def test_config_lookup_timeout_must_be_in_range(value):
    with pytest.raises(ValidationError, match="lookup_timeout"):
        Config(mode="local", priorities={1: [{"urls": ["a"]}]}, lookup_timeout=value)


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
        "webhook": {"enabled": True, "url": "https://example.com"},
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
    assert config.webhook.enabled is True
    assert config.webhook.url == "https://example.com"
    assert config.proxy_playlist == "https://custom-proxy.com"
    assert config.low_latency is False


def test_config_rejects_unknown_top_level_keys():
    """Config has no extra="ignore": an unknown top-level key fails, not a silent no-op."""
    config_data = {
        "credentials": {"username": "user", "secret": "pass"},
        "priorities": {999: [{"urls": ["streamer1"]}]},
        "skip_hosted": True,
    }
    with pytest.raises(ValidationError, match="skip_hosted"):
        Config(**config_data)


def test_config_mode_defaults_to_both():
    config = Config(
        credentials={"username": "user", "secret": "pass"},
        priorities={999: [{"urls": ["streamer1"]}]},
    )
    assert config.mode == "both"


def test_config_mode_accepts_local_and_remote():
    for m in ("local", "remote"):
        config = Config(
            credentials={"username": "user", "secret": "pass"},
            priorities={999: [{"urls": ["streamer1"]}]},
            mode=m,
        )
        assert config.mode == m


def test_config_mode_normalizes_case():
    config = Config(
        credentials={"username": "user", "secret": "pass"},
        priorities={999: [{"urls": ["streamer1"]}]},
        mode="LOCAL",
    )
    assert config.mode == "local"


def test_config_mode_rejects_invalid():
    with pytest.raises(ValidationError):
        Config(
            credentials={"username": "user", "secret": "pass"},
            priorities={999: [{"urls": ["streamer1"]}]},
            mode="offline",
        )


def test_config_local_mode_allows_missing_credentials():
    # local mode does not touch quadstream, so credentials are optional
    config = Config(
        priorities={999: [{"urls": ["streamer1"]}]},
        mode="local",
    )
    assert config.credentials is None
    assert config.mode == "local"


def test_config_non_local_requires_credentials():
    for m in ("both", "remote"):
        with pytest.raises(ValidationError):
            Config(priorities={999: [{"urls": ["streamer1"]}]}, mode=m)


def test_relay_segments_defaults_on():
    config = Config(priorities={}, mode="local")
    assert config.relay_segments is True


def test_log_level_defaults_to_none():
    config = Config(priorities={}, mode="local")
    assert config.log_level is None


def test_log_level_accepts_each_value():
    for level in ("debug", "info", "warning", "error"):
        config = Config(priorities={}, mode="local", log_level=level)
        assert config.log_level == level


def test_log_level_normalizes_case():
    config = Config(priorities={}, mode="local", log_level="WARNING")
    assert config.log_level == "warning"


def test_log_level_rejects_invalid():
    with pytest.raises(ValidationError, match="log_level"):
        Config(priorities={}, mode="local", log_level="verbose")


class TestTwitchAPI:
    BASE = {"mode": "local", "priorities": {1: [{"urls": ["a"]}]}}

    def test_unset_by_default(self):
        assert Config(**self.BASE).twitch_api is None

    def test_inline_secret(self):
        config = Config(**self.BASE, twitch_api={"client_id": "id", "client_secret": "s"})
        assert config.twitch_api is not None
        assert config.twitch_api.client_secret == "s"

    def test_secret_from_env_file_merges_with_file_client_id(self, tmp_path, monkeypatch):
        secret = tmp_path / "secret"
        secret.write_text("s3cret\n")
        monkeypatch.setenv("QL_TWITCH_API__CLIENT_SECRET_FILE", str(secret))
        config = Config(**self.BASE, twitch_api={"client_id": "id"})
        assert config.twitch_api is not None
        assert (config.twitch_api.client_id, config.twitch_api.client_secret) == ("id", "s3cret")

    def test_needs_a_secret(self):
        with pytest.raises(ValidationError, match="client_secret or client_secret_file"):
            Config(**self.BASE, twitch_api={"client_id": "id"})

    def test_unreadable_secret_file_disables_helix(self, tmp_path):
        with structlog.testing.capture_logs() as logs:
            config = Config(
                **self.BASE,
                twitch_api={"client_id": "id", "client_secret_file": str(tmp_path / "none")},
            )
        assert config.twitch_api is not None
        assert config.twitch_api.client_secret is None
        assert any("Twitch client secret" in log["event"] for log in logs)
