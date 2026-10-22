"""Quad selection algorithm with stability, diversity, and saturation."""

from dataclasses import dataclass, field

import structlog

from quadlink.config.models import Config
from quadlink.types import PrioritizedStream, Quad

logger = structlog.get_logger()

EMPTY_SLOTS = ("", "", "", "")


@dataclass(frozen=True)
class Overrides:
    """Operator adjustments applied on top of the scored selection."""


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
    """Compose the next quad from the live pool and the previous state.

    The pool is ordered best first. Channels already on screen keep their slots;
    newcomers take freed slots lowest-first in priority order. The input state
    is not modified, and an empty pool returns it as is, so slots and categories
    survive a cycle in which every lookup failed.
    """
    if not pool:
        logger.info("no stream candidates available")
        return state

    # reversed: candidates arrive best first, and the first of a duplicate channel wins
    candidate_map = {s.stream.channel: s for s in reversed(pool)}
    existing_streams = _existing_streams(state, candidate_map)
    existing_channels = {s.stream.channel for s in existing_streams}
    departed_categories = _departed_categories(state, existing_channels)

    logger.debug(
        "quad selection started",
        total_candidates=len(pool),
        existing_streams=len(existing_streams),
        departed_categories=list(departed_categories),
    )

    adjusted = _apply_adjustments(
        config, pool, existing_channels, existing_streams, departed_categories
    )
    adjusted.sort(key=lambda s: (s.priority, s.tiebreaker), reverse=True)
    selected = _select_top_4(adjusted)
    seated = _seat(selected, existing_streams)

    new = QuadState(
        quad=Quad([(s.stream.master_url or s.stream.url) if s else "" for s in seated]),
        channels=tuple(s.stream.channel if s else "" for s in seated),
        categories={s.stream.channel: s.stream.metadata.category for s in selected},
    )
    _log_changes(state, new, [s for s in seated if s])
    return new


def _existing_streams(
    state: QuadState, candidate_map: dict[str, PrioritizedStream]
) -> list[PrioritizedStream]:
    """Streams of the previous quad still in the pool, with their slots."""
    return [
        PrioritizedStream(
            stream=candidate_map[channel].stream,
            priority=candidate_map[channel].priority,
            tiebreaker=candidate_map[channel].tiebreaker,
            position=position,
        )
        for channel, position in state.positions.items()
        if channel in candidate_map
    ]


def _departed_categories(state: QuadState, existing_channels: set[str]) -> set[str]:
    """Categories of the previous quad that no remaining channel represents."""
    previous = state.categories
    departed = {previous[c] for c in previous.keys() - existing_channels}
    remaining = {previous[c] for c in existing_channels if c in previous}
    return departed - remaining


def _apply_adjustments(
    config: Config,
    candidates: list[PrioritizedStream],
    existing_channels: set[str],
    existing_streams: list[PrioritizedStream],
    departed_categories: set[str],
) -> list[PrioritizedStream]:
    """Copies of the candidates with stability, saturation, diversity and continuity applied."""
    category_counts: dict[str, int] = {}
    for stream in existing_streams:
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


def _select_top_4(candidates: list[PrioritizedStream]) -> list[PrioritizedStream]:
    """The first 4 unique channels of the sorted candidates."""
    selected = []
    seen_channels = set()

    for candidate in candidates:
        channel = candidate.stream.channel
        if channel in seen_channels:
            continue
        selected.append(candidate)
        seen_channels.add(channel)
        if len(selected) == 4:
            break

    return selected


def _seat(
    selected: list[PrioritizedStream], existing_streams: list[PrioritizedStream]
) -> list[PrioritizedStream | None]:
    """Seat the selection in slots 0-3, keeping the slots of existing streams."""
    seats: list[PrioritizedStream | None] = [None, None, None, None]
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
