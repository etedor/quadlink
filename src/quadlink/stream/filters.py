"""Regex-based stream filtering system."""

import re
from enum import StrEnum

import structlog

from quadlink.config.models import Config, Ruleset
from quadlink.types import Stream

logger = structlog.get_logger()


class RejectReason(StrEnum):
    """Machine-readable rejection reasons."""

    CATEGORY_ALLOW_MISS = "CATEGORY_ALLOW_MISS"
    CATEGORY_BLOCK_MATCH = "CATEGORY_BLOCK_MATCH"
    TITLE_ALLOW_MISS = "TITLE_ALLOW_MISS"
    TITLE_BLOCK_MATCH = "TITLE_BLOCK_MATCH"


class FilterCache:
    """Cache compiled regex patterns for performance."""

    def __init__(self) -> None:
        self._cache: dict[str, re.Pattern[str]] = {}

    def get_pattern(self, regex: str) -> re.Pattern[str]:
        """Get or compile and cache regex pattern."""
        if regex not in self._cache:
            self._cache[regex] = re.compile(regex)
        return self._cache[regex]


class StreamFilter:
    """Filters streams based on category and title regex patterns."""

    def __init__(self, config: Config):
        """
        Initialize stream filter.

        Args:
            config: Application configuration with rulesets
        """
        self.config = config
        self.cache = FilterCache()

    def apply_filters(self, stream: Stream, ruleset_names: list[str]) -> tuple[bool, str, str]:
        """
        Check if stream passes all filters from specified rulesets.

        Args:
            stream: Stream to filter
            ruleset_names: List of ruleset names to apply

        Returns:
            Tuple of (passed, reason, ruleset) - reason/ruleset are empty if passed
        """
        maybe_rulesets = [(name, self.config.get_ruleset(name)) for name in ruleset_names]
        rulesets: list[tuple[str, Ruleset]] = [
            (name, r) for name, r in maybe_rulesets if r is not None
        ]

        if not rulesets:
            return True, "", ""

        return self._check_filters_with_attribution(stream, rulesets)

    def _check_filters_with_attribution(
        self, stream: Stream, rulesets: list[tuple[str, Ruleset]]
    ) -> tuple[bool, str, str]:
        """
        Check if stream passes filter rules, tracking which ruleset caused rejection.

        Logic:
        1. Collect all allow/block patterns with their source ruleset
        2. If any allow_categories exist: category must match at least one
        3. If no allow_categories: check block_categories
        4. Same logic for titles
        5. Return which ruleset caused the rejection

        Returns:
            Tuple of (passed, reason, ruleset)
        """
        category = stream.metadata.category
        title = stream.metadata.title

        # (pattern, ruleset_name) tuples for attribution
        allow_categories: list[tuple[str, str]] = []
        allow_titles: list[tuple[str, str]] = []
        block_categories: list[tuple[str, str]] = []
        block_titles: list[tuple[str, str]] = []

        for name, ruleset in rulesets:
            for pattern in ruleset.filters.allow_categories:
                allow_categories.append((pattern, name))
            for pattern in ruleset.filters.allow_titles:
                allow_titles.append((pattern, name))
            for pattern in ruleset.filters.block_categories:
                block_categories.append((pattern, name))
            for pattern in ruleset.filters.block_titles:
                block_titles.append((pattern, name))

        if allow_categories:
            if not self._matches_any(category, [p for p, _ in allow_categories]):
                sources = ", ".join(sorted({name for _, name in allow_categories}))
                return False, RejectReason.CATEGORY_ALLOW_MISS, sources
        else:
            matched_ruleset = self._find_matching_ruleset(category, block_categories)
            if matched_ruleset:
                return False, RejectReason.CATEGORY_BLOCK_MATCH, matched_ruleset

        if allow_titles:
            if not self._matches_any(title, [p for p, _ in allow_titles]):
                sources = ", ".join(sorted({name for _, name in allow_titles}))
                return False, RejectReason.TITLE_ALLOW_MISS, sources
        else:
            matched_ruleset = self._find_matching_ruleset(title, block_titles)
            if matched_ruleset:
                return False, RejectReason.TITLE_BLOCK_MATCH, matched_ruleset

        return True, "", ""

    def _find_matching_ruleset(self, text: str, patterns: list[tuple[str, str]]) -> str | None:
        """Find which ruleset's pattern matched the text."""
        for pattern_str, ruleset_name in patterns:
            try:
                pattern = self.cache.get_pattern(pattern_str)
                if pattern.search(text):
                    return ruleset_name
            except re.error:
                continue
        return None

    def _matches_any(self, text: str, patterns: list[str]) -> bool:
        """
        Check if text matches any of the regex patterns.

        Args:
            text: Text to match against
            patterns: List of regex patterns

        Returns:
            True if any pattern matches
        """
        for pattern_str in patterns:
            try:
                pattern = self.cache.get_pattern(pattern_str)
                if pattern.search(text):
                    return True
            except re.error as e:
                logger.warning("invalid regex pattern", pattern=pattern_str, error=str(e))
                continue

        return False
