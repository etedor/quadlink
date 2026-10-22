"""Tests for the live-adjustable minimum log level."""

from unittest.mock import patch

import pytest
import structlog

from quadlink import logconfig


@pytest.fixture(autouse=True)
def reset_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logconfig, "_level_num", logconfig.LOG_LEVELS["info"])
    monkeypatch.setattr(logconfig, "_pinned", False)


def _passes(method_name: str) -> bool:
    try:
        logconfig._level_filter(None, method_name, {"event": method_name})
    except structlog.DropEvent:
        return False
    return True


class TestLevelFilter:
    def test_default_level_is_info(self) -> None:
        assert _passes("info")
        assert not _passes("debug")

    def test_set_level_changes_the_threshold(self) -> None:
        logconfig.set_level("warning")
        assert not _passes("info")
        assert _passes("warning")
        assert _passes("error")


class TestPrecedence:
    """CLI --log-level pins the level; otherwise the config value, or info."""

    def test_config_level_applies_when_not_pinned(self) -> None:
        logconfig.apply_config_level("error")
        assert not _passes("warning")
        assert _passes("error")

    def test_config_level_none_falls_back_to_info(self) -> None:
        logconfig.set_level("debug")
        logconfig.apply_config_level(None)
        assert _passes("info")
        assert not _passes("debug")

    def test_pinned_level_is_not_overridden_by_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(logconfig, "_pinned", True)
        logconfig.set_level("warning")
        logconfig.apply_config_level("debug")
        assert not _passes("info")
        assert _passes("warning")

    def test_setup_logging_with_a_level_pins(self) -> None:
        with patch("quadlink.logconfig.structlog"):
            logconfig.setup_logging("debug")
        assert logconfig._pinned is True
        assert _passes("debug")

    def test_setup_logging_without_a_level_does_not_pin(self) -> None:
        with patch("quadlink.logconfig.structlog"):
            logconfig.setup_logging()
        assert logconfig._pinned is False
