"""Read-only view of the quad and every configured channel, for the dashboard."""

from dataclasses import dataclass
from enum import StrEnum

from quadlink.quad import Overrides, QuadState
from quadlink.types import ChannelState, ChannelStatus


class DashboardState(StrEnum):
    """Where a configured channel stands, as the dashboard shows it."""

    IN_QUAD = "in quad"
    BENCHED = "benched"
    WAITING = "waiting"
    LIVE = "live"
    OFFLINE = "offline"
    NO_VIDEO = "no video"
    REJECTED = "rejected"
    LOOKUP_FAILED = "lookup failed"


@dataclass(frozen=True)
class SlotView:
    """One tile.

    Attributes:
        slot: Slot number 0-3.
        channel: Channel on screen, "" for an empty slot.
        display_name: Twitch display name of that channel, or its login when
            the last refresh did not resolve it.
        pinned: The channel on screen is pinned to this slot.
    """

    slot: int
    channel: str
    display_name: str
    pinned: bool


@dataclass(frozen=True)
class ChannelView:
    """One configured channel.

    Attributes:
        channel: Channel key (lowercase Twitch login).
        state: Dashboard state.
        slot: Slot it is on screen in, if any.
        pinned: It has a pin, whether or not it is on screen.
        status: The last refresh's lookup outcome.
    """

    channel: str
    state: DashboardState
    slot: int | None
    pinned: bool
    status: ChannelStatus


@dataclass(frozen=True)
class Snapshot:
    """The quad and every configured channel at one moment.

    Attributes:
        slots: The four tiles in slot order.
        channels: Every configured channel, in config order.
        refreshed_at: Wall-clock time (epoch seconds) of the last refresh.
    """

    slots: list[SlotView]
    channels: list[ChannelView]
    refreshed_at: float | None


def snapshot(
    statuses: list[ChannelStatus],
    state: QuadState,
    overrides: Overrides,
    waiting: list[str],
    refreshed_at: float | None,
) -> Snapshot:
    """Build the dashboard view from the daemon's current state."""
    names = {s.channel: s.stream.metadata.author for s in statuses if s.stream is not None}
    slots = [
        SlotView(
            slot=slot,
            channel=channel,
            display_name=names.get(channel, channel),
            pinned=bool(channel) and overrides.pins.get(channel) == slot,
        )
        for slot, channel in enumerate(state.channels)
    ]
    positions = state.positions
    channels = [
        ChannelView(
            channel=s.channel,
            state=_state(s, positions, overrides, waiting),
            slot=positions.get(s.channel),
            pinned=s.channel in overrides.pins,
            status=s,
        )
        for s in statuses
    ]
    return Snapshot(slots=slots, channels=channels, refreshed_at=refreshed_at)


def _state(
    status: ChannelStatus,
    positions: dict[str, int],
    overrides: Overrides,
    waiting: list[str],
) -> DashboardState:
    if status.channel in positions:
        return DashboardState.IN_QUAD
    if status.channel in overrides.benches:
        return DashboardState.BENCHED
    if status.state is ChannelState.LIVE and status.channel in waiting:
        return DashboardState.WAITING
    return DashboardState(status.state.value)
