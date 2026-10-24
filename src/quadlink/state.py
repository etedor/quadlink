"""Persistence of overrides and slot positions across daemon restarts."""

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import structlog
from pydantic import BaseModel, Field

from quadlink.quad import Hold, Overrides, QuadState

logger = structlog.get_logger()

DEFAULT_STATE_PATH = Path("/var/lib/quadlink/state.json")
STATE_VERSION = 4

Slot = Annotated[int, Field(ge=0, le=3)]


class _HoldFile(BaseModel):
    holder: str
    replaced: str | None


class _StateFile(BaseModel):
    version: Literal[4]
    holds: dict[Slot, _HoldFile]
    absent: dict[str, int]
    channels: Annotated[list[str], Field(min_length=4, max_length=4)]
    categories: dict[str, str]


class StateStore:
    """A JSON file holding the overrides and the sticky slot positions.

    Playlist URLs are not kept: they expire, and the first refresh mints new ones.
    """

    def __init__(self, path: Path):
        self.path = path
        # text of the last write or load, so unchanged state is not written again
        self._written: str | None = None
        # the last write failed; the warning is logged once per streak
        self._failing = False

    def load(self) -> tuple[Overrides, QuadState]:
        """The saved state; empty when the file is missing, unreadable or invalid."""
        empty = Overrides(), QuadState()
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return empty
        except OSError as e:
            logger.warning(
                "state file unreadable, starting empty", path=str(self.path), error=str(e)
            )
            return empty
        try:
            data = _StateFile.model_validate_json(text)
            overrides = Overrides(
                holds={s: Hold(h.holder, h.replaced) for s, h in data.holds.items()},
                absent=data.absent,
            )
        except ValueError as e:
            logger.warning("state file invalid, starting empty", path=str(self.path), error=str(e))
            return empty
        state = QuadState(channels=tuple(data.channels), categories=data.categories)
        self._written = _serialize(overrides, state)
        logger.info("state loaded", path=str(self.path), pins=dict(overrides.pins))
        return overrides, state

    def save(self, overrides: Overrides, state: QuadState) -> None:
        """Write the state atomically if it changed; a failed write is logged."""
        text = _serialize(overrides, state)
        if text == self._written:
            return
        try:
            _write_atomic(self.path, text)
        except OSError as e:
            if not self._failing:
                logger.warning("state file write failed", path=str(self.path), error=str(e))
                self._failing = True
            return
        self._failing = False
        self._written = text


def _serialize(overrides: Overrides, state: QuadState) -> str:
    return json.dumps(
        {
            "version": STATE_VERSION,
            # added channels end with the process, and their holds with them
            "holds": {
                str(slot): {"holder": hold.holder, "replaced": hold.replaced}
                for slot, hold in sorted(overrides.holds.items())
                if hold.holder not in overrides.extras
            },
            "absent": {
                c: n for c, n in sorted(overrides.absent.items()) if c not in overrides.extras
            },
            "channels": list(state.channels),
            "categories": dict(sorted(state.categories.items())),
        },
        indent=2,
    )


def _write_atomic(path: Path, text: str) -> None:
    """Replace path with text, so a crash leaves the old file or the new one whole."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
