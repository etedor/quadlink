"""Operator actions on tile holds, and their expiry.

A drop makes a hold: the tile shows the dropped channel, and the channel the
first drop put off the tile waits for it (see quad.Hold). Ending a hold returns
the tile to what it showed before the first drop and never changes another tile.

Every function is pure: it takes the overrides and the quad state and returns new
ones. Invalid input raises OverrideError with a message fit for the operator.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import replace

from quadlink.quad import Hold, Overrides, QuadState
from quadlink.types import Quad

# refreshes (about 30 s each) a hold outlives its holder leaving the pool; within
# them a holder that returns takes its tile back
GRACE_REFRESHES = 2

# Twitch logins are 4-25 of [a-z0-9_] today, but legacy accounts go down to 2
_LOGIN = re.compile(r"[a-z0-9_]{2,25}")

Result = tuple[Overrides, QuadState]


class OverrideError(ValueError):
    """An operator action the overrides cannot apply."""


def place(
    overrides: Overrides,
    state: QuadState,
    channel: str,
    slot: int,
    configured: Collection[str],
) -> Result:
    """Make channel hold the tile.

    A channel shown on another tile swaps with this one instead, and one already
    on this tile changes nothing. A tile keeps what it showed before the first
    drop; a later drop sends the previous holder back to selection, unless the
    hold remembers nothing, when the previous holder is remembered instead. An
    added channel is never replaced: it reaches the quad only as a holder.
    """
    _check_slot(slot)
    _check_channel(channel, configured)
    on = state.positions.get(channel)
    if on == slot:
        return overrides, state
    if on is not None:
        return swap(overrides, state, on, slot)
    holds = {
        s: Hold(h.holder, None if h.replaced == channel else h.replaced)
        for s, h in overrides.holds.items()
    }
    for held in [s for s, h in holds.items() if h.holder == channel]:
        holds, state = _end(holds, state, held)
    old = holds.get(slot)
    # the stream before the first drop, else (none remembered, or it was forgotten
    # offline) the one this drop puts off the tile
    replaced = (old.replaced if old is not None and old.replaced else state.channels[slot]) or None
    if replaced in overrides.extras:
        replaced = None
    holds[slot] = Hold(channel, replaced)
    return _build(overrides, holds, fresh={channel}), state


def swap(overrides: Overrides, state: QuadState, a: int, b: int) -> Result:
    """Swap the channels of two slots; a hold moves with its holder, list and all."""
    _check_slot(a)
    _check_slot(b)
    if a == b:
        return overrides, state
    holds = {s: h for s, h in overrides.holds.items() if s not in (a, b)}
    if a in overrides.holds:
        holds[b] = overrides.holds[a]
    if b in overrides.holds:
        holds[a] = overrides.holds[b]
    channels = list(state.channels)
    urls = state.quad.to_list()
    channels[a], channels[b] = channels[b], channels[a]
    urls[a], urls[b] = urls[b], urls[a]
    new_state = replace(state, quad=Quad(urls), channels=tuple(channels))
    return _build(overrides, holds), new_state


def unpin(overrides: Overrides, state: QuadState, slot: int) -> Result:
    """End the tile's hold: the first channel it replaced gets the tile back."""
    _check_slot(slot)
    if slot not in overrides.holds:
        return overrides, state
    holds, state = _end(dict(overrides.holds), state, slot)
    return _build(overrides, holds), state


def unpin_all(overrides: Overrides, state: QuadState) -> Result:
    """End every hold; added channels stay added."""
    holds = dict(overrides.holds)
    for slot in list(holds):
        holds, state = _end(holds, state, slot)
    return _build(overrides, holds), state


def put_back(overrides: Overrides, state: QuadState, channel: str) -> Result:
    """Unpin the tile whose hold replaced channel."""
    slot = overrides.replaced_at(channel)
    if slot is None:
        raise OverrideError(f"{channel} was not replaced on any tile")
    return unpin(overrides, state, slot)


def add_extra(overrides: Overrides, channel: str, configured: Collection[str]) -> Overrides:
    """Add a channel beside the config; the caller has checked it is live."""
    if channel in configured or channel in overrides.extras:
        raise OverrideError(f"{channel} is already in the list")
    if not _LOGIN.fullmatch(channel):
        raise OverrideError(f"{channel!r} is not a Twitch channel name")
    return replace(overrides, extras=overrides.extras | {channel})


def remove_extra(overrides: Overrides, state: QuadState, channel: str) -> Result:
    """Remove an added channel; a tile it holds goes back as Unpin gives it."""
    if channel not in overrides.extras:
        raise OverrideError(f"{channel} is not an added channel")
    holds = dict(overrides.holds)
    for slot in [s for s, h in holds.items() if h.holder == channel]:
        holds, state = _end(holds, state, slot)
    return replace(_build(overrides, holds), extras=overrides.extras - {channel}), state


def drop_unconfigured(
    overrides: Overrides, state: QuadState, configured: Collection[str]
) -> Result:
    """End the holds of channels no longer configured and unlist them."""
    holds = {
        s: Hold(h.holder, h.replaced if h.replaced in configured else None)
        for s, h in overrides.holds.items()
    }
    for slot in [s for s, h in holds.items() if h.holder not in configured]:
        holds, state = _end(holds, state, slot)
    return _build(overrides, holds), state


def age(overrides: Overrides, state: QuadState, pool: Collection[str]) -> Result:
    """Count one refresh for every holder and replaced channel out of the pool.

    A channel back in the pool starts over at zero. A holder out for more than
    GRACE_REFRESHES refreshes loses its tile as Unpin takes it; a replaced channel
    out that long is forgotten, so the hold, when it ends, leaves the tile to
    selection instead of keeping an offline stream's claim on it.
    """
    watched = [c for h in overrides.holds.values() for c in (h.holder, h.replaced) if c]
    absent = {c: overrides.absent.get(c, 0) + 1 for c in watched if c not in pool}
    gone = {c for c, n in absent.items() if n > GRACE_REFRESHES}
    holds = {
        s: Hold(h.holder, None if h.replaced in gone else h.replaced)
        for s, h in overrides.holds.items()
    }
    for slot in [s for s, h in holds.items() if h.holder in gone]:
        holds, state = _end(holds, state, slot)
    kept = {c for h in holds.values() for c in (h.holder, h.replaced) if c}
    return (
        Overrides(
            holds=holds,
            absent={c: n for c, n in absent.items() if c in kept},
            extras=overrides.extras,
        ),
        state,
    )


def _end(holds: dict[int, Hold], state: QuadState, slot: int) -> tuple[dict[int, Hold], QuadState]:
    """Drop a hold and seat the channel it replaced on the tile.

    Seated there, that channel keeps the tile as any on-screen channel does; one
    out of the pool leaves it to selection.
    """
    hold = holds.pop(slot)
    if hold.replaced is None:
        return holds, state
    channels = list(state.channels)
    urls = state.quad.to_list()
    channels[slot] = hold.replaced
    # the next composition mints its playlist URL
    urls[slot] = ""
    return holds, replace(state, quad=Quad(urls), channels=tuple(channels))


def _build(
    previous: Overrides,
    holds: Mapping[int, Hold],
    fresh: Collection[str] = (),
) -> Overrides:
    """Overrides with absence counts kept for surviving holds' channels, except fresh ones."""
    keep = {c for h in holds.values() for c in (h.holder, h.replaced) if c} - set(fresh)
    return Overrides(
        holds=dict(holds),
        absent={c: n for c, n in previous.absent.items() if c in keep},
        extras=previous.extras,
    )


def _check_slot(slot: int) -> None:
    if not isinstance(slot, int) or isinstance(slot, bool) or slot not in range(4):
        raise OverrideError(f"slot {slot!r} does not exist: use a slot from 0 to 3")


def _check_channel(channel: str, configured: Collection[str]) -> None:
    if channel not in configured:
        raise OverrideError(f"channel {channel!r} is not in the config")
