"""Operator actions on pins and benches, and their expiry.

Every function is pure: it takes the current overrides (and quad state where the
action depends on what is on screen) and returns new ones. Invalid input raises
OverrideError with a message fit for the operator.
"""

from collections.abc import Collection, Mapping
from dataclasses import replace

from quadlink.quad import Overrides, QuadState
from quadlink.types import Quad

# refreshes (about 30 s each) a pin or bench outlives its channel leaving the pool;
# within them a pinned channel that returns takes its slot back
GRACE_REFRESHES = 2


class OverrideError(ValueError):
    """An operator action the overrides cannot apply."""


def place(
    overrides: Overrides,
    state: QuadState,
    channel: str,
    slot: int,
    configured: Collection[str],
) -> Overrides:
    """Pin channel to slot; bench the channel on screen there."""
    _check_slot(slot)
    _check_channel(channel, configured)
    occupant = state.channels[slot]
    pins = {c: s for c, s in overrides.pins.items() if c != channel and s != slot}
    pins[channel] = slot
    benches = set(overrides.benches) - {channel}
    bound = dict(overrides.bound)
    if occupant and occupant != channel:
        pins.pop(occupant, None)
        benches.add(occupant)
        bound[occupant] = channel
    return _build(overrides, pins, benches, fresh={channel, occupant}, bound=bound)


def swap(overrides: Overrides, state: QuadState, a: int, b: int) -> tuple[Overrides, QuadState]:
    """Swap the channels of two slots; a channel's pin moves with it."""
    _check_slot(a)
    _check_slot(b)
    if a == b:
        return overrides, state
    moved = ((state.channels[a], b), (state.channels[b], a))
    moving = {c: s for c, s in moved if c in overrides.pins}
    # a pin moving in replaces one held on that slot for a channel out of the pool
    pins = {c: s for c, s in overrides.pins.items() if c not in moving and s not in moving.values()}
    pins |= moving
    channels = list(state.channels)
    urls = state.quad.to_list()
    channels[a], channels[b] = channels[b], channels[a]
    urls[a], urls[b] = urls[b], urls[a]
    new_state = replace(state, quad=Quad(urls), channels=tuple(channels))
    return _build(overrides, pins, overrides.benches), new_state


def pin(overrides: Overrides, state: QuadState, slot: int) -> Overrides:
    """Pin the channel on screen in slot to that slot."""
    _check_slot(slot)
    channel = state.channels[slot]
    if not channel:
        raise OverrideError(f"slot {slot} is empty: there is no channel to pin")
    pins = {c: s for c, s in overrides.pins.items() if c != channel and s != slot}
    pins[channel] = slot
    return _build(overrides, pins, set(overrides.benches) - {channel}, fresh={channel})


def unpin(overrides: Overrides, slot: int) -> Overrides:
    """Remove the pin on slot, if any; its channel stays until selection displaces it."""
    _check_slot(slot)
    pins = {c: s for c, s in overrides.pins.items() if s != slot}
    return _build(overrides, pins, overrides.benches)


def bench(overrides: Overrides, channel: str, configured: Collection[str]) -> Overrides:
    """Keep channel out of the quad; its pin, if any, is removed.

    A bench set this way is the operator's own, so it is bound to no pin.
    """
    _check_channel(channel, configured)
    pins = {c: s for c, s in overrides.pins.items() if c != channel}
    bound = {b: p for b, p in overrides.bound.items() if b != channel}
    return _build(overrides, pins, overrides.benches | {channel}, fresh={channel}, bound=bound)


def unbench(overrides: Overrides, channel: str, configured: Collection[str]) -> Overrides:
    """Make channel eligible again; it returns by priority."""
    _check_channel(channel, configured)
    return _build(overrides, overrides.pins, overrides.benches - {channel})


def reset() -> Overrides:
    """Clear every pin and bench."""
    return Overrides()


def drop_unconfigured(overrides: Overrides, configured: Collection[str]) -> Overrides:
    """Clear the pins and benches of channels no longer in the config."""
    pins = {c: s for c, s in overrides.pins.items() if c in configured}
    return _build(overrides, pins, {c for c in overrides.benches if c in configured})


def age(overrides: Overrides, pool: Collection[str]) -> Overrides:
    """Count one refresh for every pinned or benched channel out of the pool.

    A channel back in the pool starts over at zero; one out for more than
    GRACE_REFRESHES refreshes loses its pin or bench.
    """
    absent = {c: overrides.absent.get(c, 0) + 1 for c in overrides.channels if c not in pool}
    expired = {c for c, n in absent.items() if n > GRACE_REFRESHES}
    pins = {c: s for c, s in overrides.pins.items() if c not in expired}
    benches, bound = _release(pins, overrides.benches - expired, overrides.bound)
    return Overrides(
        pins=pins,
        benches=benches,
        absent={c: n for c, n in absent.items() if c in pins.keys() | benches},
        bound=bound,
    )


def _release(
    pins: Mapping[str, int], benches: Collection[str], bound: Mapping[str, str]
) -> tuple[frozenset[str], dict[str, str]]:
    """Benches and bindings left once every bench bound to an ended pin ends too."""
    bound = {b: p for b, p in bound.items() if b in benches}
    released = {b for b, p in bound.items() if p not in pins}
    return (
        frozenset(set(benches) - released),
        {b: p for b, p in bound.items() if b not in released},
    )


def _build(
    previous: Overrides,
    pins: Mapping[str, int],
    benches: Collection[str],
    fresh: Collection[str] = (),
    bound: Mapping[str, str] | None = None,
) -> Overrides:
    """Overrides with absence counts kept for surviving entries, except fresh ones.

    `bound` defaults to the previous bindings; benches bound to a pin that is
    gone are released.
    """
    kept, bindings = _release(pins, benches, previous.bound if bound is None else bound)
    keep = (pins.keys() | kept) - set(fresh)
    return Overrides(
        pins=dict(pins),
        benches=kept,
        absent={c: n for c, n in previous.absent.items() if c in keep},
        bound=bindings,
    )


def _check_slot(slot: int) -> None:
    if not isinstance(slot, int) or isinstance(slot, bool) or slot not in range(4):
        raise OverrideError(f"slot {slot!r} does not exist: use a slot from 0 to 3")


def _check_channel(channel: str, configured: Collection[str]) -> None:
    if channel not in configured:
        raise OverrideError(f"channel {channel!r} is not in the config")
