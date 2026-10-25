"""Quad selection algorithm with stability, diversity, and saturation."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

import structlog

from quadlink.config.models import Config
from quadlink.types import PrioritizedStream, Quad

logger = structlog.get_logger()

EMPTY_SLOTS = ("", "", "", "")


@dataclass(frozen=True)
class Hold:
    """A tile the operator filled by a drop or locked.

    A hard hold keeps the tile until its holder goes offline. A soft hold
    competes for it as an on-screen stream scored as the stream it replaced
    (see quad._soft_score), and ends when it loses.

    Attributes:
        holder: The channel the tile shows while it is in the pool.
        replaced: The channel on the tile before the first drop. It takes no
            tile while the hold lasts; when a hard hold ends, it gets the tile back.
        soft: The hold is soft.
        priority: The replaced channel's priority when the hold was made; 0
            when the hold replaced nothing.
    """

    holder: str
    replaced: str | None = None
    soft: bool = False
    priority: int = 0


@dataclass(frozen=True)
class Overrides:
    """Operator adjustments applied on top of the scored selection.

    Attributes:
        holds: Slot (0-3) -> its hold.
        absent: Consecutive refreshes each held or replaced channel has been out of the pool; see
            quadlink.overrides.age.
        extras: Channels outside the config that hold a tile. They are looked up
            like configured ones while they hold it, and every one is a holder.
    """

    holds: Mapping[int, Hold] = field(default_factory=dict)
    absent: Mapping[str, int] = field(default_factory=dict)
    extras: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if bad := [slot for slot in self.holds if slot not in range(4)]:
            raise ValueError(f"hold slots out of range 0-3: {bad}")
        holders = [h.holder for h in self.holds.values()]
        if len(set(holders)) != len(holders):
            raise ValueError(f"a channel holds more than one tile: {holders}")
        listed = [h.replaced for h in self.holds.values() if h.replaced]
        if len(set(listed)) != len(listed):
            raise ValueError(f"a channel is replaced on more than one tile: {listed}")
        if both := set(holders) & set(listed):
            raise ValueError(f"channels both holding and replaced: {sorted(both)}")
        if listed_extras := set(listed) & self.extras:
            raise ValueError(f"outside channels are never replaced: {sorted(listed_extras)}")
        if idle := self.extras - set(holders):
            raise ValueError(f"outside channels must hold a tile: {sorted(idle)}")
        if stray := [h.holder for h in self.holds.values() if h.priority and not h.replaced]:
            raise ValueError(f"holds with a priority but nothing replaced: {stray}")

    @property
    def pins(self) -> dict[str, int]:
        """Slot of each holder."""
        return {h.holder: slot for slot, h in self.holds.items()}

    @property
    def hard_pins(self) -> dict[str, int]:
        """Slot of each holder of a hard hold."""
        return {h.holder: slot for slot, h in self.holds.items() if not h.soft}

    @property
    def replaced(self) -> set[str]:
        """Every channel a hold replaced."""
        return {h.replaced for h in self.holds.values() if h.replaced}


@dataclass(frozen=True)
class QuadState:
    """The outcome of one composition, and the input to the next.

    Attributes:
        quad: Playlist URL per slot 0-3.
        channels: Channel (lowercase Twitch login) per slot 0-3, "" for an empty
            slot. The URL is re-minted every refresh (fresh Twitch token/edge),
            so the channel is what identifies a slot's stream.
        categories: Category of each on-screen channel when it was selected.
    """

    quad: Quad = field(default_factory=Quad)
    channels: tuple[str, ...] = EMPTY_SLOTS
    categories: dict[str, str] = field(default_factory=dict)

    @property
    def positions(self) -> dict[str, int]:
        """Slot (0-3) of each on-screen channel."""
        return {channel: slot for slot, channel in enumerate(self.channels) if channel}


def compose(
    pool: list[PrioritizedStream],
    state: QuadState,
    overrides: Overrides,
    config: Config,
) -> QuadState:
    """Compose the next quad from the live pool, the previous state and the overrides.

    The pool is ordered best first. Replaced channels drop out of it; hard
    holders in it take their slots; soft holders in it compete for theirs as
    on-screen streams; the other slots fill by adjusted priority.
    Channels already on screen keep their slots; newcomers take freed slots
    lowest-first in priority order. The input state is not modified, and an empty
    pool returns it as is, so slots and categories survive a cycle in which every
    lookup failed.
    """
    if not pool:
        logger.info("no stream candidates available")
        return state
    new, on_screen = _compose(pool, state, overrides, config)
    _log_changes(state, new, on_screen)
    return new


def _eligible(channel: str, overrides: Overrides) -> bool:
    if channel in overrides.replaced:
        return False
    return channel not in overrides.extras or channel in overrides.pins


def _compose(
    pool: list[PrioritizedStream],
    state: QuadState,
    overrides: Overrides,
    config: Config,
) -> tuple[QuadState, list[PrioritizedStream]]:
    """The composed state and its on-screen streams in slot order."""
    # reversed: candidates arrive best first, and the first of a duplicate channel wins
    candidate_map = {
        s.stream.channel: s for s in reversed(pool) if _eligible(s.stream.channel, overrides)
    }
    pinned = _reclaimed(overrides, candidate_map)
    soft = {
        h.holder: (slot, h)
        for slot, h in overrides.holds.items()
        if h.soft and h.holder in candidate_map
    }
    existing_streams = _existing_streams(
        state, candidate_map, pinned, {c: slot for c, (slot, _) in soft.items()}
    )
    existing_channels = {s.stream.channel for s in existing_streams}
    departed_categories = _departed_categories(state, existing_channels, pinned)

    logger.debug(
        "quad selection started",
        total_candidates=len(pool),
        existing_streams=len(existing_streams),
        departed_categories=list(departed_categories),
    )

    competitors = [
        _soft_score(s, soft[s.stream.channel][1], config) if s.stream.channel in soft else s
        for s in pool
        if s.stream.channel in candidate_map and s.stream.channel not in pinned
    ]
    adjusted = _apply_adjustments(
        config,
        competitors,
        existing_channels,
        [*existing_streams, *pinned.values()],
        departed_categories,
    )
    adjusted.sort(key=lambda s: (s.priority, s.tiebreaker), reverse=True)
    selected = _select_top(adjusted, 4 - len(pinned))
    seated = _seat(selected, existing_streams, pinned.values())

    new = QuadState(
        quad=Quad([(s.stream.master_url or s.stream.url) if s else "" for s in seated]),
        channels=tuple(s.stream.channel if s else "" for s in seated),
        categories={
            s.stream.channel: s.stream.metadata.category for s in [*selected, *pinned.values()]
        },
    )
    return new, [s for s in seated if s]


def _reclaimed(
    overrides: Overrides, candidate_map: dict[str, PrioritizedStream]
) -> dict[str, PrioritizedStream]:
    """Hard-pinned channels in the pool, each positioned at its pinned slot.

    A hard-pinned channel takes its slot back whenever it is in the pool, from
    whichever channel holds the slot meanwhile.
    """
    return {
        channel: replace(candidate_map[channel], position=slot)
        for channel, slot in overrides.hard_pins.items()
        if channel in candidate_map
    }


def _existing_streams(
    state: QuadState,
    candidate_map: dict[str, PrioritizedStream],
    pinned: dict[str, PrioritizedStream],
    soft: Mapping[str, int],
) -> list[PrioritizedStream]:
    """Unpinned streams of the previous quad still in the pool, with their slots.

    A soft holder in the pool counts as on screen at its held slot, even while
    back from an absence within the grace. The occupant of a slot a holder
    reclaims is left out: it competes as a newcomer, with no stability bonus,
    so it cannot push out a channel that has held its tile all along.
    """
    held = {s.position for s in pinned.values()} | set(soft.values())
    positions = {
        channel: position
        for channel, position in state.positions.items()
        if channel not in pinned and channel not in soft and position not in held
    }
    return [
        PrioritizedStream(
            stream=candidate_map[channel].stream,
            priority=candidate_map[channel].priority,
            tiebreaker=candidate_map[channel].tiebreaker,
            position=position,
        )
        for channel, position in {**positions, **soft}.items()
        if channel in candidate_map
    ]


def _soft_score(candidate: PrioritizedStream, hold: Hold, config: Config) -> PrioritizedStream:
    """The soft holder's candidate, scored as what it replaced plus pin_bonus.

    With nothing replaced, its own priority stands in. The stability bonus comes
    on top, as for any on-screen stream, so anything that beats replaced priority
    + stability_bonus + pin_bonus takes the tile.
    """
    base = hold.priority if hold.replaced else candidate.priority
    return replace(candidate, priority=base + config.pin_bonus)


def _departed_categories(
    state: QuadState, existing_channels: set[str], pinned: dict[str, PrioritizedStream]
) -> set[str]:
    """Categories of the previous quad that no remaining or pinned channel represents."""
    previous = state.categories
    departed = {previous[c] for c in previous.keys() - existing_channels - pinned.keys()}
    remaining = {previous[c] for c in existing_channels if c in previous}
    remaining |= {s.stream.metadata.category for s in pinned.values()}
    return departed - remaining


def _apply_adjustments(
    config: Config,
    candidates: list[PrioritizedStream],
    existing_channels: set[str],
    present: list[PrioritizedStream],
    departed_categories: set[str],
) -> list[PrioritizedStream]:
    """Copies of the candidates with stability, saturation, diversity and continuity applied.

    present is every stream that stays on screen, pinned ones included; their
    categories count toward saturation.
    """
    category_counts: dict[str, int] = {}
    for stream in present:
        category = stream.stream.metadata.category
        category_counts[category] = category_counts.get(category, 0) + 1

    seen_categories: set[str] = set()

    adjusted = []
    for candidate in candidates:
        channel = candidate.stream.channel
        category = candidate.stream.metadata.category
        adjusted_priority = candidate.priority

        # 1. stability bonus
        if channel in existing_channels:
            adjusted_priority += config.stability_bonus
            logger.debug(
                "stability bonus applied",
                author=candidate.stream.metadata.author,
                bonus=config.stability_bonus,
                new_priority=adjusted_priority,
            )

        # 2. category saturation penalty (only for NEW streams, not existing ones)
        # existing streams shouldn't be penalized for their own presence
        if category in category_counts and channel not in existing_channels:
            count = category_counts[category]
            # graduated penalty based on how many streams already in this category
            if count == 1:
                penalty = config.diversity_bonus // 3
            elif count == 2:
                penalty = (2 * config.diversity_bonus) // 3
            else:  # count >= 3
                penalty = config.diversity_bonus

            adjusted_priority -= penalty
            logger.debug(
                "saturation penalty applied",
                author=candidate.stream.metadata.author,
                category=category,
                count=count,
                penalty=penalty,
                new_priority=adjusted_priority,
            )

        # 3. diversity bonus (for NEW categories only, and only once per category)
        if category not in category_counts and category not in seen_categories:
            adjusted_priority += config.diversity_bonus
            seen_categories.add(category)
            logger.debug(
                "diversity bonus applied",
                author=candidate.stream.metadata.author,
                category=category,
                bonus=config.diversity_bonus,
                new_priority=adjusted_priority,
            )

        # 4. category continuity bonus (for replacements matching departed categories)
        if channel not in existing_channels and category in departed_categories:
            adjusted_priority += config.category_continuity_bonus
            logger.debug(
                "category continuity bonus applied",
                author=candidate.stream.metadata.author,
                category=category,
                bonus=config.category_continuity_bonus,
                new_priority=adjusted_priority,
            )

        adjusted.append(
            PrioritizedStream(
                stream=candidate.stream,
                priority=adjusted_priority,
                tiebreaker=candidate.tiebreaker,
                position=candidate.position,
            )
        )

    return adjusted


def _select_top(candidates: list[PrioritizedStream], count: int) -> list[PrioritizedStream]:
    """The first count unique channels of the sorted candidates.

    This is the eviction rule: a channel that ranks higher displaces the lowest
    unpinned channel on screen. Pinned channels are not candidates.
    """
    selected: list[PrioritizedStream] = []
    seen_channels = set()

    for candidate in candidates:
        if len(selected) == count:
            break
        channel = candidate.stream.channel
        if channel in seen_channels:
            continue
        selected.append(candidate)
        seen_channels.add(channel)

    return selected


def _seat(
    selected: list[PrioritizedStream],
    existing_streams: list[PrioritizedStream],
    pinned: Iterable[PrioritizedStream],
) -> list[PrioritizedStream | None]:
    """Seat pinned streams in their slots, then the selection, keeping existing slots."""
    seats: list[PrioritizedStream | None] = [None, None, None, None]
    for stream in pinned:
        assert stream.position is not None
        seats[stream.position] = stream
    selected_map = {s.stream.channel: s for s in selected}

    kept = set()
    for stream in existing_streams:
        channel = stream.stream.channel
        if channel in selected_map and stream.position is not None:
            seats[stream.position] = selected_map[channel]
            kept.add(channel)

    # selected is already priority-ordered, so the best newcomer takes the lowest free slot
    newcomers = iter(s for s in selected if s.stream.channel not in kept)
    for i in range(4):
        if seats[i] is None:
            seats[i] = next(newcomers, None)

    return seats


def _log_changes(previous: QuadState, new: QuadState, on_screen: list[PrioritizedStream]) -> None:
    """Log the quad and any change from the previous composition."""
    names = [s.stream.metadata.author for s in on_screen]

    if not previous.positions:
        logger.info("quad", streams=names)
        return

    prev_channels = [c for c in previous.channels if c]
    if prev_channels == [c for c in new.channels if c]:
        logger.debug("quad unchanged", streams=names)
        return

    added = new.positions.keys() - set(prev_channels)
    removed = set(prev_channels) - new.positions.keys()

    if added or removed:
        logger.info("quad", streams=names, added=list(added), removed=list(removed))
    else:
        logger.info("quad", streams=names)
