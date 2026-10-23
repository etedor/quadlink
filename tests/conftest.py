"""Shared fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every daemon at a fresh state file, never /var/lib/quadlink."""
    path = tmp_path / "state.json"
    monkeypatch.setenv("QL_STATE_FILE", str(path))
    return path
