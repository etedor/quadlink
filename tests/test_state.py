"""Tests for the state file."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog

from quadlink.quad import Hold, Overrides, QuadState
from quadlink.state import StateStore

OVERRIDES = Overrides(holds={3: Hold("f", ("d", "e"))}, absent={"f": 1})
STATE = QuadState(channels=("a", "b", "", "f"), categories={"a": "Games", "b": "Chess"})


def test_round_trip(state_file: Path):
    StateStore(state_file).save(OVERRIDES, STATE)
    overrides, state = StateStore(state_file).load()
    assert overrides == OVERRIDES
    assert state.channels == STATE.channels
    assert state.categories == STATE.categories
    assert json.loads(state_file.read_text())["version"] == 3


def test_missing_file_starts_empty(state_file: Path):
    with structlog.testing.capture_logs() as logs:
        assert StateStore(state_file).load() == (Overrides(), QuadState())
    assert logs == []


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        json.dumps({"version": 4, "holds": {}}),
        json.dumps(
            {
                "version": 3,
                "holds": {"0": {"holder": "a", "replaced": ["a"]}},
                "absent": {},
                "channels": ["a", "", "", ""],
                "categories": {},
            }
        ),
        json.dumps(
            {
                "version": 3,
                "holds": {"9": {"holder": "a", "replaced": []}},
                "absent": {},
                "channels": ["a", "", "", ""],
                "categories": {},
            }
        ),
    ],
    ids=["corrupt", "unknown version", "holder listed", "slot out of range"],
)
def test_invalid_file_is_logged_and_ignored(state_file: Path, text: str):
    state_file.write_text(text)
    with structlog.testing.capture_logs() as logs:
        assert StateStore(state_file).load() == (Overrides(), QuadState())
    assert [e["event"] for e in logs] == ["state file invalid, starting empty"]


def test_write_is_atomic(state_file: Path):
    store = StateStore(state_file)
    store.save(OVERRIDES, STATE)
    before = state_file.read_text()
    with (
        patch("quadlink.state.os.replace", side_effect=OSError("disk full")),
        structlog.testing.capture_logs() as logs,
    ):
        store.save(Overrides(), STATE)
        store.save(Overrides(), QuadState())
    assert state_file.read_text() == before
    assert list(state_file.parent.iterdir()) == [state_file]
    # once per failure streak
    assert [e["event"] for e in logs] == ["state file write failed"]
    store.save(Overrides(), STATE)
    assert StateStore(state_file).load()[0] == Overrides()


def test_write_is_synced_before_the_rename(state_file: Path):
    calls: list[str] = []
    with (
        patch("quadlink.state.os.fsync", side_effect=lambda fd: calls.append("fsync")),
        patch(
            "quadlink.state.os.replace",
            side_effect=lambda src, dst: calls.append("replace"),
        ),
    ):
        StateStore(state_file).save(OVERRIDES, STATE)
    assert calls == ["fsync", "replace"]


def test_unchanged_state_is_not_written_again(state_file: Path):
    store = StateStore(state_file)
    with patch("quadlink.state._write_atomic") as write:
        store.save(OVERRIDES, STATE)
        store.save(OVERRIDES, STATE)
    assert write.call_count == 1


def test_loaded_state_is_not_written_back(state_file: Path):
    StateStore(state_file).save(OVERRIDES, STATE)
    store = StateStore(state_file)
    overrides, state = store.load()
    with patch("quadlink.state._write_atomic") as write:
        store.save(overrides, state)
    write.assert_not_called()


def test_holds_by_added_channels_are_not_saved(state_file: Path):
    overrides = Overrides(
        holds={0: Hold("xy", ("a",)), 3: Hold("f", ("d",))},
        absent={"xy": 1},
        extras=frozenset({"xy"}),
    )
    StateStore(state_file).save(overrides, STATE)
    loaded, _ = StateStore(state_file).load()
    assert loaded == Overrides(holds={3: Hold("f", ("d",))})
