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
        self.previous_quad: Quad | None = None
        self.previous_categories: dict[str, str] = {}  # author -> category
        self.previous_positions: dict[str, int] = {}  # author -> position (0-3)

    def build_quad(self, candidates: list[PrioritizedStream]) -> Quad:
        """
        Build optimal quad from stream candidates.

        Algorithm:
        1. Preserve existing streams and track positions
        2. Apply stability bonus to existing streams
        3. Apply category saturation penalty
        4. Apply diversity bonus to new categories
        5. Select top 4 unique authors
        6. Preserve positions from previous quad
        7. Log changes

        Args:
            candidates: List of PrioritizedStream objects (sorted by priority)

        Returns:
            Quad with selected streams
        """
        if not candidates:
            logger.info("no stream candidates available")
            return Quad()

        candidate_map = {s.stream.metadata.author.lower(): s for s in candidates}
        existing_streams = self._get_existing_streams(candidate_map)
        existing_authors = {s.stream.metadata.author.lower() for s in existing_streams}
        departed_categories = self._get_departed_categories(existing_authors)

        logger.debug(
            "quad selection started",
            total_candidates=len(candidates),
            existing_streams=len(existing_streams),
            departed_categories=list(departed_categories),
        )

        adjusted_candidates = self._apply_adjustments(
            candidates, existing_authors, existing_streams, departed_categories
        )
        adjusted_candidates.sort(key=lambda s: (s.priority, s.tiebreaker), reverse=True)
        selected = self._select_top_4(adjusted_candidates)
        quad = self._build_quad_with_positions(selected, existing_streams)
        self._log_changes(quad, selected)

        self.previous_quad = quad
        self.previous_categories = {
            s.stream.metadata.author.lower(): s.stream.metadata.category for s in selected
        }
        self.previous_positions = self._build_position_map(quad, selected)

        return quad

    def _build_position_map(self, quad: Quad, selected: list[PrioritizedStream]) -> dict[str, int]:
        """
        Build author -> position mapping from quad.

        Args:
            quad: The built quad
            selected: Selected streams with metadata

        Returns:
            Dict mapping author (lowercase) to position (0-3)
        """
        url_to_author = {}
        for s in selected:
            url = s.stream.master_url or s.stream.url
            url_to_author[url] = s.stream.metadata.author.lower()

        position_map = {}
        for position, url in enumerate(quad.to_dict().values()):
            if url and url in url_to_author:
                position_map[url_to_author[url]] = position

        return position_map

    def _get_existing_streams(
        self, candidate_map: dict[str, PrioritizedStream]
    ) -> list[PrioritizedStream]:
        """
        Get streams from previous quad that are still available.

        Args:
            candidate_map: Author -> PrioritizedStream mapping

        Returns:
            List of streams from previous quad (with positions)
        """
        if not self.previous_positions:
            return []

        existing = []
        for author, position in self.previous_positions.items():
            if author in candidate_map:
                stream = candidate_map[author]
                stream_with_pos = PrioritizedStream(
                    stream=stream.stream,
                    priority=stream.priority,
                    tiebreaker=stream.tiebreaker,
                    position=position,
                )
                existing.append(stream_with_pos)

        return existing

    def _get_departed_categories(self, existing_authors: set[str]) -> set[str]:
        """
        Find categories from previous quad that are no longer available.

        Args:
            existing_authors: Authors still available from previous quad

        Returns:
            Set of categories that lost their streams
        """
        if not self.previous_categories:
            return set()

        departed_authors = set(self.previous_categories.keys()) - existing_authors
        departed_categories = {self.previous_categories[author] for author in departed_authors}

        # exclude categories still represented in remaining streams
        existing_categories = {
            self.previous_categories[author]
            for author in existing_authors
            if author in self.previous_categories
        }

        return departed_categories - existing_categories

    def _apply_adjustments(
        self,
        candidates: list[PrioritizedStream],
        existing_authors: set[str],
        existing_streams: list[PrioritizedStream],
        departed_categories: set[str],
    ) -> list[PrioritizedStream]:
        """
        Apply stability bonus, saturation penalty, diversity bonus, and continuity bonus.

        Args:
            candidates: All stream candidates
            existing_authors: Set of authors currently in quad
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
            author = candidate.stream.metadata.author.lower()
            category = candidate.stream.metadata.category
            adjusted_priority = candidate.priority

            # 1. stability bonus
            if author in existing_authors:
                adjusted_priority += self.config.stability_bonus
                logger.debug(
                    "stability bonus applied",
                    author=candidate.stream.metadata.author,
                    bonus=self.config.stability_bonus,
                    new_priority=adjusted_priority,
                )

            # 2. category saturation penalty (only for NEW streams, not existing ones)
            # existing streams shouldn't be penalized for their own presence
            if category in category_counts and author not in existing_authors:
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
            if author not in existing_authors and category in departed_categories:
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
        Select top 4 unique authors from candidates.

        Args:
            candidates: Sorted candidates (by adjusted priority + tiebreaker)

        Returns:
            Top 4 streams (unique authors)
        """
        selected = []
        seen_authors = set()

        for candidate in candidates:
            author = candidate.stream.metadata.author.lower()

            if author in seen_authors:
                continue

            selected.append(candidate)
            seen_authors.add(author)

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
        selected_map = {
            s.stream.metadata.author.lower(): s.stream.master_url or s.stream.url for s in selected
        }

        existing_authors = set()
        for stream in existing_streams:
            author = stream.stream.metadata.author.lower()
            if author in selected_map and stream.position is not None:
                quad_urls[stream.position] = selected_map[author]
                existing_authors.add(author)

        new_streams = [
            s for s in selected if s.stream.metadata.author.lower() not in existing_authors
        ]
        # sort by category, author for deterministic ordering
        new_streams.sort(key=lambda s: (s.stream.metadata.category, s.stream.metadata.author))

        new_stream_index = 0
        for i in range(4):
            if not quad_urls[i] and new_stream_index < len(new_streams):
                new_stream = new_streams[new_stream_index].stream
                quad_urls[i] = new_stream.master_url or new_stream.url
                new_stream_index += 1

        return Quad(
            stream1=quad_urls[0],
            stream2=quad_urls[1],
            stream3=quad_urls[2],
            stream4=quad_urls[3],
        )

    def _log_changes(self, new_quad: Quad, selected: list[PrioritizedStream]) -> None:
        """
        Log the quad and any changes from previous.

        Args:
            new_quad: Newly built quad
            selected: Selected streams with metadata
        """
        # Get authors in position order (stream1, stream2, stream3, stream4)
        new_authors_list = self._get_authors_in_position_order(new_quad, selected)

        if not self.previous_positions:
            logger.info("quad", streams=new_authors_list)
            return

        prev_authors = set(self.previous_positions.keys())
        new_authors = {s.stream.metadata.author.lower() for s in selected}

        added = new_authors - prev_authors
        removed = prev_authors - new_authors

        if added or removed:
            logger.info(
                "quad",
                streams=new_authors_list,
                added=list(added),
                removed=list(removed),
            )
        else:
            logger.info("quad", streams=new_authors_list)

    def _get_authors_in_position_order(
        self, quad: Quad, selected: list[PrioritizedStream]
    ) -> list[str]:
        """
        Get author names in quad position order.

        Args:
            quad: The built quad
            selected: Selected streams with metadata

        Returns:
            List of author names in position order [stream1, stream2, stream3, stream4]
        """
        url_to_author = {}
        for s in selected:
            url = s.stream.master_url or s.stream.url
            url_to_author[url] = s.stream.metadata.author

        authors = []
        for url in [quad.stream1, quad.stream2, quad.stream3, quad.stream4]:
            if url:
                authors.append(url_to_author[url])
        return authors
