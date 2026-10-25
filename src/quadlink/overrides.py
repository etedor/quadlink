"""Operator actions on tile holds, and their expiry.

A drop makes a hard hold: the tile shows the dropped channel, and the channel
the first drop put off the tile waits for it (see quad.Hold). Ending a hard hold
returns the tile to what it showed before the first drop and never changes
another tile. A lock makes a hold soft; a soft hold that ends forgets what it
replaced and leaves the tile to selection.

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
    priorities: Mapping[str, int] | None = None,
) -> Result:
    """Make channel hold the tile, hard.

    A channel shown on another tile swaps with this one instead, and one already
    on this tile changes nothing. A tile keeps what it showed before the first
    drop; a later drop sends the previous holder back to selection, unless the
    hold remembers nothing, when the previous holder is remembered instead. A
    channel outside configured joins the extras for as long as it holds a tile;
    it is never replaced, since it reaches the quad only as a holder. priorities
    gives a newly replaced channel's priority (default 0) for the hold to keep.
    """
    _check_slot(slot)
    outside = channel not in configured
    if outside and not is_login(channel):
        raise OverrideError(f"{channel!r} is not a Twitch channel name")
    on = state.positions.get(channel)
    if on == slot:
        return overrides, state
    if on is not None:
        return swap(overrides, state, on, slot)
    holds = {s: _forget(h) if h.replaced == channel else h for s, h in overrides.holds.items()}
    for held in [s for s, h in holds.items() if h.holder == channel]:
        holds, state = _end(holds, state, held)
    old = holds.get(slot)
    if old is not None and old.replaced:
        holds[slot] = Hold(channel, old.replaced, priority=old.priority)
    else:
        # none remembered, or it was forgotten offline: the one this drop puts off the tile
        replaced = state.channels[slot] or None
        if replaced is None or replaced in overrides.extras:
            holds[slot] = Hold(channel)
        else:
            holds[slot] = Hold(channel, replaced, priority=(priorities or {}).get(replaced, 0))
    extras = overrides.extras | {channel} if outside else overrides.extras
    return _build(overrides, holds, fresh={channel}, extras=extras), state


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


def lock(overrides: Overrides, state: QuadState, slot: int, soft: bool) -> Result:
    """Make the tile's hold soft or hard; a tile with no hold gets one for its channel."""
    _check_slot(slot)
    hold = overrides.holds.get(slot)
    if hold is None:
        channel = state.channels[slot]
        if not channel:
            raise OverrideError(f"slot {slot} is empty: there is no stream to pin")
        hold = Hold(channel, soft=soft)
    elif hold.soft == soft:
        return overrides, state
    else:
        hold = replace(hold, soft=soft)
    return _build(overrides, {**overrides.holds, slot: hold}), state


def lose(overrides: Overrides, state: QuadState, pool: Collection[str]) -> Overrides:
    """End every soft hold whose holder is in the pool but off its tile in state.

    Composition puts a soft holder in the pool on its tile unless a stream
    outscored it there; that stream keeps the tile, and the replaced channel is
    forgotten. A soft holder out of the pool keeps its hold through the grace.
    """
    lost = [
        slot
        for slot, h in overrides.holds.items()
        if h.soft and h.holder in pool and state.channels[slot] != h.holder
    ]
    if not lost:
        return overrides
    return _build(overrides, {s: h for s, h in overrides.holds.items() if s not in lost})


def unpin(overrides: Overrides, state: QuadState, slot: int) -> Result:
    """End the tile's hold: for a hard hold, the first channel it replaced gets the tile back."""
    _check_slot(slot)
    if slot not in overrides.holds:
        return overrides, state
    holds, state = _end(dict(overrides.holds), state, slot)
    return _build(overrides, holds), state


def unpin_all(overrides: Overrides, state: QuadState) -> Result:
    """End every hold; outside channels leave with theirs."""
    holds = dict(overrides.holds)
    for slot in list(holds):
        holds, state = _end(holds, state, slot)
    return _build(overrides, holds), state


def drop_unconfigured(
    overrides: Overrides, state: QuadState, configured: Collection[str]
) -> Result:
    """End the holds of channels no longer configured and unlist them."""
    holds = {
        s: h if h.replaced is None or h.replaced in configured else _forget(h)
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
    holds = {s: _forget(h) if h.replaced in gone else h for s, h in overrides.holds.items()}
    for slot in [s for s, h in holds.items() if h.holder in gone]:
        holds, state = _end(holds, state, slot)
    kept = {c for h in holds.values() for c in (h.holder, h.replaced) if c}
    return (
        Overrides(
            holds=holds,
            absent={c: n for c, n in absent.items() if c in kept},
            extras=overrides.extras & {h.holder for h in holds.values()},
        ),
        state,
    )


def _end(holds: dict[int, Hold], state: QuadState, slot: int) -> tuple[dict[int, Hold], QuadState]:
    """Drop a hold; a hard one seats the channel it replaced on the tile.

    Seated there, that channel keeps the tile as any on-screen channel does; one
    out of the pool leaves it to selection.
    """
    hold = holds.pop(slot)
    if hold.replaced is None or hold.soft:
        return holds, state
    channels = list(state.channels)
    urls = state.quad.to_list()
    channels[slot] = hold.replaced
    # the next composition mints its playlist URL
    urls[slot] = ""
    return holds, replace(state, quad=Quad(urls), channels=tuple(channels))


def _forget(hold: Hold) -> Hold:
    # the priority stays: a soft hold keeps the standing of what it replaced
    return replace(hold, replaced=None)


def _build(
    previous: Overrides,
    holds: Mapping[int, Hold],
    fresh: Collection[str] = (),
    extras: frozenset[str] | None = None,
) -> Overrides:
    """Overrides with absence counts kept for surviving holds' channels, except fresh ones.

    An extra that no longer holds a tile leaves the extras.
    """
    keep = {c for h in holds.values() for c in (h.holder, h.replaced) if c} - set(fresh)
    holders = {h.holder for h in holds.values()}
    return Overrides(
        holds=dict(holds),
        absent={c: n for c, n in previous.absent.items() if c in keep},
        extras=(previous.extras if extras is None else extras) & holders,
    )


def is_login(channel: str) -> bool:
    """Whether channel can be a Twitch login."""
    return _LOGIN.fullmatch(channel) is not None


def _check_slot(slot: int) -> None:
    if not isinstance(slot, int) or isinstance(slot, bool) or slot not in range(4):
        raise OverrideError(f"slot {slot!r} does not exist: use a slot from 0 to 3")
