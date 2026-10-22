"""Quad selection algorithm with stability, diversity, and saturation."""

import structlog

from quadlink.config.models import Config
from quadlink.types import PrioritizedStream, Quad

logger = structlog.get_logger()


class QuadBuilder:
    """Select optimal 4-stream combinations with weighted scoring."""

    def __init__(self, config: Config):
        """
        Initialize quad builder.

        Args:
            config: Application configuration with bonuses and penalties
        """
        self.config = config
        # keyed by channel (lowercase Twitch login)
        self.previous_categories: dict[str, str] = {}  # channel -> category
        self.previous_positions: dict[str, int] = {}  # channel -> position (0-3)
        self.quad_changed: bool = False
        # stable channel identity per slot (position-aligned with the quad),
        # so the relay can tell a token refresh from a real stream switch
        self.slot_identities: list[str] = ["", "", "", ""]

    def build_quad(self, candidates: list[PrioritizedStream]) -> Quad:
        """
        Build optimal quad from stream candidates.

        Algorithm:
        1. Preserve existing streams and track positions
        2. Apply stability bonus to existing streams
        3. Apply category saturation penalty
        4. Apply diversity bonus to new categories
        5. Select top 4 unique channels
        6. Preserve positions from previous quad
        7. Log changes

        Args:
            candidates: List of PrioritizedStream objects (sorted by priority)

        Returns:
            Quad with selected streams
        """
        if not candidates:
            logger.info("no stream candidates available")
            self.slot_identities = ["", "", "", ""]
            return Quad()

        # reversed: candidates arrive best first, and the first of a duplicate channel wins
        candidate_map = {s.stream.channel: s for s in reversed(candidates)}
        existing_streams = self._get_existing_streams(candidate_map)
        existing_channels = {s.stream.channel for s in existing_streams}
        departed_categories = self._get_departed_categories(existing_channels)

        logger.debug(
            "quad selection started",
            total_candidates=len(candidates),
            existing_streams=len(existing_streams),
            departed_categories=list(departed_categories),
        )

        adjusted_candidates = self._apply_adjustments(
            candidates, existing_channels, existing_streams, departed_categories
        )
        adjusted_candidates.sort(key=lambda s: (s.priority, s.tiebreaker), reverse=True)
        selected = self._select_top_4(adjusted_candidates)
        quad = self._build_quad_with_positions(selected, existing_streams)
        self._log_changes(quad, selected)

        self.previous_categories = {s.stream.channel: s.stream.metadata.category for s in selected}
        self.previous_positions = self._build_position_map(quad, selected)
        self.slot_identities = self._build_slot_identities(quad, selected)

        return quad

    def _build_position_map(self, quad: Quad, selected: list[PrioritizedStream]) -> dict[str, int]:
        """
        Build channel -> position mapping from quad.

        Args:
            quad: The built quad
            selected: Selected streams with metadata

        Returns:
            Dict mapping channel to position (0-3)
        """
        url_to_channel = {}
        for s in selected:
            url = s.stream.master_url or s.stream.url
            url_to_channel[url] = s.stream.channel

        position_map = {}
        for position, url in enumerate(quad.to_list()):
            if url and url in url_to_channel:
                position_map[url_to_channel[url]] = position

        return position_map

    def _build_slot_identities(self, quad: Quad, selected: list[PrioritizedStream]) -> list[str]:
        """
        Build a position-aligned list of stable channel identities.

        The playlist URL a slot holds is re-minted every cycle (fresh Twitch
        token/edge), so it cannot identify a stream. The channel is stable, so
        the relay uses it to distinguish a token refresh from a real switch.

        Args:
            quad: The built quad
            selected: Selected streams with metadata

        Returns:
            List of 4 channel identities, aligned with quad slots ("" if empty)
        """
        url_to_identity = {}
        for s in selected:
            key = s.stream.master_url or s.stream.url
            url_to_identity[key] = s.stream.channel

        return [url_to_identity.get(url, "") if url else "" for url in quad.to_list()]

    def _get_existing_streams(
        self, candidate_map: dict[str, PrioritizedStream]
    ) -> list[PrioritizedStream]:
        """
        Get streams from previous quad that are still available.

        Args:
            candidate_map: Channel -> PrioritizedStream mapping

        Returns:
            List of streams from previous quad (with positions)
        """
        if not self.previous_positions:
            return []

        existing = []
        for channel, position in self.previous_positions.items():
            if channel in candidate_map:
                stream = candidate_map[channel]
                stream_with_pos = PrioritizedStream(
                    stream=stream.stream,
                    priority=stream.priority,
                    tiebreaker=stream.tiebreaker,
                    position=position,
                )
                existing.append(stream_with_pos)

        return existing

    def _get_departed_categories(self, existing_channels: set[str]) -> set[str]:
        """
        Find categories from previous quad that are no longer available.

        Args:
            existing_channels: Channels still available from previous quad

        Returns:
            Set of categories that lost their streams
        """
        if not self.previous_categories:
            return set()

        departed_channels = set(self.previous_categories.keys()) - existing_channels
        departed_categories = {self.previous_categories[channel] for channel in departed_channels}

        # exclude categories still represented in remaining streams
        existing_categories = {
            self.previous_categories[channel]
            for channel in existing_channels
            if channel in self.previous_categories
        }

        return departed_categories - existing_categories

    def _apply_adjustments(
        self,
        candidates: list[PrioritizedStream],
        existing_channels: set[str],
        existing_streams: list[PrioritizedStream],
        departed_categories: set[str],
    ) -> list[PrioritizedStream]:
        """
        Apply stability bonus, saturation penalty, diversity bonus, and continuity bonus.

        Args:
            candidates: All stream candidates
            existing_channels: Set of channels currently in quad
            existing_streams: List of existing streams with positions
            departed_categories: Categories that lost their streams

        Returns:
            New list of candidates with adjusted priorities
        """
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
                adjusted_priority += self.config.stability_bonus
                logger.debug(
                    "stability bonus applied",
                    author=candidate.stream.metadata.author,
                    bonus=self.config.stability_bonus,
                    new_priority=adjusted_priority,
                )

            # 2. category saturation penalty (only for NEW streams, not existing ones)
            # existing streams shouldn't be penalized for their own presence
            if category in category_counts and channel not in existing_channels:
                count = category_counts[category]
                # graduated penalty based on how many streams already in this category
                if count == 1:
                    penalty = self.config.diversity_bonus // 3
                elif count == 2:
                    penalty = (2 * self.config.diversity_bonus) // 3
                else:  # count >= 3
                    penalty = self.config.diversity_bonus

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
                adjusted_priority += self.config.diversity_bonus
                seen_categories.add(category)
                logger.debug(
                    "diversity bonus applied",
                    author=candidate.stream.metadata.author,
                    category=category,
                    bonus=self.config.diversity_bonus,
                    new_priority=adjusted_priority,
                )

            # 4. category continuity bonus (for replacements matching departed categories)
            if channel not in existing_channels and category in departed_categories:
                adjusted_priority += self.config.category_continuity_bonus
                logger.debug(
                    "category continuity bonus applied",
                    author=candidate.stream.metadata.author,
                    category=category,
                    bonus=self.config.category_continuity_bonus,
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

    def _select_top_4(self, candidates: list[PrioritizedStream]) -> list[PrioritizedStream]:
        """
        Select top 4 unique channels from candidates.

        Args:
            candidates: Sorted candidates (by adjusted priority + tiebreaker)

        Returns:
            Top 4 streams (unique channels)
        """
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

    def _build_quad_with_positions(
        self, selected: list[PrioritizedStream], existing_streams: list[PrioritizedStream]
    ) -> Quad:
        """
        Build quad preserving positions of existing streams.

        Args:
            selected: Top 4 selected streams
            existing_streams: Streams from previous quad with positions

        Returns:
            Quad with URLs in proper positions
        """
        quad_urls = ["", "", "", ""]

        # prefer playlist over front-facing URL for playback
        selected_map = {s.stream.channel: s.stream.master_url or s.stream.url for s in selected}

        existing_channels = set()
        for stream in existing_streams:
            channel = stream.stream.channel
            if channel in selected_map and stream.position is not None:
                quad_urls[stream.position] = selected_map[channel]
                existing_channels.add(channel)

        # selected is already priority-ordered, so the best newcomer takes the lowest free slot
        new_streams = [s for s in selected if s.stream.channel not in existing_channels]

        new_stream_index = 0
        for i in range(4):
            if not quad_urls[i] and new_stream_index < len(new_streams):
                new_stream = new_streams[new_stream_index].stream
                quad_urls[i] = new_stream.master_url or new_stream.url
                new_stream_index += 1

        return Quad(quad_urls)

    def _log_changes(self, new_quad: Quad, selected: list[PrioritizedStream]) -> None:
        """
        Log the quad and any changes from previous.

        Args:
            new_quad: Newly built quad
            selected: Selected streams with metadata
        """
        on_screen = self._in_position_order(new_quad, selected)
        names = [s.stream.metadata.author for s in on_screen]

        if not self.previous_positions:
            self.quad_changed = True
            logger.info("quad", streams=names)
            return

        # quad changed if channel composition or order differs
        prev_ordered = sorted(self.previous_positions.items(), key=lambda x: x[1])
        prev_channels = [channel for channel, _ in prev_ordered]
        self.quad_changed = prev_channels != [s.stream.channel for s in on_screen]

        if not self.quad_changed:
            logger.debug("quad unchanged", streams=names)
            return

        added = {s.stream.channel for s in selected} - set(prev_channels)
        removed = set(prev_channels) - {s.stream.channel for s in selected}

        if added or removed:
            logger.info("quad", streams=names, added=list(added), removed=list(removed))
        else:
            logger.info("quad", streams=names)

    def _in_position_order(
        self, quad: Quad, selected: list[PrioritizedStream]
    ) -> list[PrioritizedStream]:
        """The selected streams in quad slot order, skipping empty slots."""
        by_url = {s.stream.master_url or s.stream.url: s for s in selected}
        return [by_url[url] for url in quad.to_list() if url]
