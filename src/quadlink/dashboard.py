"""Read-only view of the quad and every configured channel, for the dashboard."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from quadlink.quad import Overrides, QuadState
from quadlink.types import ChannelState, ChannelStatus
from quadlink.viewers import Stats


class DashboardState(StrEnum):
    """Where a configured channel stands, as the dashboard shows it."""

    IN_QUAD = "in quad"
    LIVE = "live"
    OFFLINE = "offline"
    NO_VIDEO = "no video"
    REJECTED = "rejected"
    LOOKUP_FAILED = "lookup failed"


class QuadStreamLogin(StrEnum):
    """Where the daemon stands with quadstream.tv."""

    OFF = "off"
    PENDING = "pending"
    LOGGED_IN = "logged in"
    FAILED = "failed"


@dataclass(frozen=True)
class SlotView:
    """One tile.

    Attributes:
        slot: Slot number 0-3.
        channel: Channel on screen, "" for an empty slot.
        display_name: Twitch display name of that channel, or its login when
            no refresh has resolved it.
        pinned: The channel on screen holds this tile.
        reserved_for: The tile's holder while it is out of the pool; it takes the
            tile back if it returns within the grace.
    """

    slot: int
    channel: str
    display_name: str
    pinned: bool
    reserved_for: str | None


@dataclass(frozen=True)
class ChannelView:
    """One configured channel.

    Attributes:
        channel: Channel key (lowercase Twitch login).
        display_name: Twitch display name, or the login when no refresh has resolved it.
        state: Dashboard state.
        slot: Slot it is on screen in, if any.
        pin_slot: Slot it holds, whether or not it is on screen.
        live: The last lookup found it live.
        extra: Added beside the config from the dashboard, until removed or a restart.
        status: The last refresh's lookup outcome.
        viewers: Viewer count, while the channel is live and a provider reports it.
        started_at: When the stream started (UTC), under the same condition.
    """

    channel: str
    display_name: str
    state: DashboardState
    slot: int | None
    pin_slot: int | None
    live: bool
    extra: bool
    status: ChannelStatus
    viewers: int | None = None
    started_at: datetime | None = None


@dataclass(frozen=True)
class Snapshot:
    """The quad and every configured channel at one moment.

    Attributes:
        slots: The four tiles in slot order.
        channels: Every configured channel, in config order.
        refreshed_at: Wall-clock time (epoch seconds) of the last refresh.
        quadstream: Login state with quadstream.tv.
    """

    slots: list[SlotView]
    channels: list[ChannelView]
    refreshed_at: float | None
    quadstream: QuadStreamLogin = QuadStreamLogin.OFF


def snapshot(
    statuses: list[ChannelStatus],
    state: QuadState,
    overrides: Overrides,
    refreshed_at: float | None,
    quadstream: QuadStreamLogin = QuadStreamLogin.OFF,
    stats: dict[str, Stats] | None = None,
    known_names: dict[str, str] | None = None,
) -> Snapshot:
    """Build the dashboard view from the daemon's current state.

    known_names holds display names from earlier refreshes, for channels the
    last refresh found without a stream.
    """
    stats = stats or {}
    names = {
        **(known_names or {}),
        **{s.channel: s.stream.metadata.author for s in statuses if s.stream is not None},
    }
    holds = overrides.holds
    slots = []
    for slot, channel in enumerate(state.channels):
        hold = holds.get(slot)
        holder = hold.holder if hold else None
        slots.append(
            SlotView(
                slot=slot,
                channel=channel,
                display_name=names.get(channel, channel),
                pinned=bool(channel) and holder == channel,
                reserved_for=None if holder == channel else holder,
            )
        )
    positions = state.positions
    channels = [
        ChannelView(
            channel=s.channel,
            display_name=names.get(s.channel, s.channel),
            state=_state(s, positions),
            slot=positions.get(s.channel),
            pin_slot=overrides.pins.get(s.channel),
            live=s.state is ChannelState.LIVE,
            extra=s.channel in overrides.extras,
            status=s,
            viewers=found.viewers if found else None,
            started_at=found.started_at if found else None,
        )
        for s in statuses
        for found in [_live_stats(s, stats)]
    ]
    return Snapshot(
        slots=slots, channels=channels, refreshed_at=refreshed_at, quadstream=quadstream
    )


def _live_stats(status: ChannelStatus, stats: dict[str, Stats]) -> Stats | None:
    # the lookup has the last word on offline: the stats can lag a stream's end
    if status.state is ChannelState.OFFLINE:
        return None
    return stats.get(status.channel)


def _state(status: ChannelStatus, positions: dict[str, int]) -> DashboardState:
    if status.channel in positions:
        return DashboardState.IN_QUAD
    return DashboardState(status.state.value)
