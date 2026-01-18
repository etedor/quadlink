"""Tests for stream filtering."""

import pytest

from quadlink.config.models import Config, Credentials, Filters, Ruleset
from quadlink.stream.filters import FilterCache, RejectReason, StreamFilter
from quadlink.types import Metadata, Stream


def make_stream(category: str, title: str) -> Stream:
    """Helper to create a stream with given category and title."""
    return Stream(
        url="https://twitch.tv/test",
        metadata=Metadata(author="test", category=category, title=title),
    )


def make_config(rulesets: list[Ruleset]) -> Config:
    """Helper to create a config with given rulesets."""
    return Config(
        credentials=Credentials(username="test", secret="test"),
        rulesets=rulesets,
        priorities={},
    )


class TestFilterCache:
    """Tests for FilterCache."""

    def test_get_pattern_compiles_and_caches(self):
        """Pattern should be compiled and cached."""
        cache = FilterCache()
        pattern1 = cache.get_pattern("^test$")
        pattern2 = cache.get_pattern("^test$")

        # should be the same object (cached)
        assert pattern1 is pattern2
        assert pattern1.match("test")
        assert not pattern1.match("other")

    def test_get_pattern_different_patterns(self):
        """Different patterns should be cached separately."""
        cache = FilterCache()
        pattern1 = cache.get_pattern("^foo$")
        pattern2 = cache.get_pattern("^bar$")

        assert pattern1 is not pattern2
        assert pattern1.match("foo")
        assert pattern2.match("bar")


class TestStreamFilterNoRulesets:
    """Tests for filtering with no rulesets."""

    def test_no_rulesets_passes(self):
        """Stream should pass when no rulesets specified."""
        config = make_config([])
        stream_filter = StreamFilter(config)
        stream = make_stream("Any Category", "Any Title")

        passed, reason, ruleset = stream_filter.apply_filters(stream, [])
        assert passed is True
        assert reason == ""
        assert ruleset == ""

    def test_nonexistent_ruleset_passes(self):
        """Stream should pass when specified ruleset doesn't exist."""
        config = make_config([])
        stream_filter = StreamFilter(config)
        stream = make_stream("Any Category", "Any Title")

        passed, reason, ruleset = stream_filter.apply_filters(stream, ["nonexistent"])
        assert passed is True
        assert reason == ""
        assert ruleset == ""


class TestStreamFilterAllowCategories:
    """Tests for allow_categories filtering."""

    @pytest.fixture
    def filter_with_allow_categories(self):
        """Filter that allows only Destiny 2 and Apex Legends."""
        config = make_config(
            [
                Ruleset(
                    name="gaming",
                    filters=Filters(
                        allow_categories=["^Destiny 2$", "^Apex Legends$"],
                    ),
                )
            ]
        )
        return StreamFilter(config)

    def test_allow_category_matches(self, filter_with_allow_categories):
        """Stream should pass when category matches allow list."""
        stream = make_stream("Destiny 2", "Playing some Destiny")
        passed, reason, ruleset = filter_with_allow_categories.apply_filters(stream, ["gaming"])

        assert passed is True
        assert reason == ""
        assert ruleset == ""

    def test_allow_category_no_match(self, filter_with_allow_categories):
        """Stream should fail when category doesn't match allow list."""
        stream = make_stream("Minecraft", "Building stuff")
        passed, reason, ruleset = filter_with_allow_categories.apply_filters(stream, ["gaming"])

        assert passed is False
        assert reason == RejectReason.CATEGORY_ALLOW_MISS
        assert ruleset == "gaming"

    def test_allow_category_partial_match(self, filter_with_allow_categories):
        """Regex should match correctly - partial match shouldn't work with anchors."""
        stream = make_stream("Destiny 2: Lightfall", "Playing DLC")
        passed, reason, ruleset = filter_with_allow_categories.apply_filters(stream, ["gaming"])

        # "^Destiny 2$" won't match "Destiny 2: Lightfall"
        assert passed is False


class TestStreamFilterBlockCategories:
    """Tests for block_categories filtering."""

    @pytest.fixture
    def filter_with_block_categories(self):
        """Filter that blocks Minecraft and Just Chatting."""
        config = make_config(
            [
                Ruleset(
                    name="global",
                    filters=Filters(
                        block_categories=["^Minecraft$", "^Just Chatting$"],
                    ),
                )
            ]
        )
        return StreamFilter(config)

    def test_block_category_matches(self, filter_with_block_categories):
        """Stream should fail when category matches block list."""
        stream = make_stream("Minecraft", "Building stuff")
        passed, reason, ruleset = filter_with_block_categories.apply_filters(stream, ["global"])

        assert passed is False
        assert reason == RejectReason.CATEGORY_BLOCK_MATCH
        assert ruleset == "global"

    def test_block_category_no_match(self, filter_with_block_categories):
        """Stream should pass when category doesn't match block list."""
        stream = make_stream("Destiny 2", "Playing Destiny")
        passed, reason, ruleset = filter_with_block_categories.apply_filters(stream, ["global"])

        assert passed is True
        assert reason == ""
        assert ruleset == ""


class TestStreamFilterAllowTitles:
    """Tests for allow_titles filtering."""

    @pytest.fixture
    def filter_with_allow_titles(self):
        """Filter that allows only titles containing 'trials'."""
        config = make_config(
            [
                Ruleset(
                    name="trials",
                    filters=Filters(
                        allow_titles=["(?i).*trials.*"],
                    ),
                )
            ]
        )
        return StreamFilter(config)

    def test_allow_title_matches(self, filter_with_allow_titles):
        """Stream should pass when title matches allow list."""
        stream = make_stream("Destiny 2", "Trials of Osiris - Going Flawless!")
        passed, reason, ruleset = filter_with_allow_titles.apply_filters(stream, ["trials"])

        assert passed is True
        assert reason == ""
        assert ruleset == ""

    def test_allow_title_case_insensitive(self, filter_with_allow_titles):
        """Case insensitive regex should work."""
        stream = make_stream("Destiny 2", "TRIALS CARRY")
        passed, reason, ruleset = filter_with_allow_titles.apply_filters(stream, ["trials"])

        assert passed is True

    def test_allow_title_no_match(self, filter_with_allow_titles):
        """Stream should fail when title doesn't match allow list."""
        stream = make_stream("Destiny 2", "Raiding with the boys")
        passed, reason, ruleset = filter_with_allow_titles.apply_filters(stream, ["trials"])

        assert passed is False
        assert reason == RejectReason.TITLE_ALLOW_MISS
        assert ruleset == "trials"


class TestStreamFilterBlockTitles:
    """Tests for block_titles filtering."""

    @pytest.fixture
    def filter_with_block_titles(self):
        """Filter that blocks titles with #ad or rerun."""
        config = make_config(
            [
                Ruleset(
                    name="global",
                    filters=Filters(
                        block_titles=["(?i).*#ad.*", "(?i).*rerun.*"],
                    ),
                )
            ]
        )
        return StreamFilter(config)

    def test_block_title_matches(self, filter_with_block_titles):
        """Stream should fail when title matches block list."""
        stream = make_stream("Destiny 2", "Playing Destiny #ad #sponsored")
        passed, reason, ruleset = filter_with_block_titles.apply_filters(stream, ["global"])

        assert passed is False
        assert reason == RejectReason.TITLE_BLOCK_MATCH
        assert ruleset == "global"

    def test_block_title_no_match(self, filter_with_block_titles):
        """Stream should pass when title doesn't match block list."""
        stream = make_stream("Destiny 2", "Live Trials gameplay")
        passed, reason, ruleset = filter_with_block_titles.apply_filters(stream, ["global"])

        assert passed is True
        assert reason == ""
        assert ruleset == ""


class TestStreamFilterMultipleRulesets:
    """Tests for combining multiple rulesets."""

    @pytest.fixture
    def multi_ruleset_filter(self):
        """Filter with multiple rulesets."""
        config = make_config(
            [
                Ruleset(
                    name="global",
                    filters=Filters(
                        block_categories=["^Just Chatting$"],
                        block_titles=["(?i).*#ad.*"],
                    ),
                ),
                Ruleset(
                    name="destiny",
                    filters=Filters(
                        allow_categories=["^Destiny 2$"],
                    ),
                ),
            ]
        )
        return StreamFilter(config)

    def test_allow_takes_precedence_over_block(self, multi_ruleset_filter):
        """When allow_categories exist, block_categories are ignored."""
        # global blocks Just Chatting, but destiny allows only Destiny 2
        # allow should take precedence
        stream = make_stream("Just Chatting", "Chatting with viewers")
        passed, reason, ruleset = multi_ruleset_filter.apply_filters(stream, ["global", "destiny"])

        # fails because category not in allow list, not because it's blocked
        assert passed is False
        assert reason == RejectReason.CATEGORY_ALLOW_MISS

    def test_combined_rulesets_category_allowed(self, multi_ruleset_filter):
        """Stream passes when category matches combined allow list."""
        stream = make_stream("Destiny 2", "Playing Destiny")
        passed, reason, ruleset = multi_ruleset_filter.apply_filters(stream, ["global", "destiny"])

        assert passed is True

    def test_combined_rulesets_title_blocked(self, multi_ruleset_filter):
        """Stream fails when title matches block list even if category allowed."""
        stream = make_stream("Destiny 2", "Destiny stream #ad")
        passed, reason, ruleset = multi_ruleset_filter.apply_filters(stream, ["global", "destiny"])

        assert passed is False
        assert reason == RejectReason.TITLE_BLOCK_MATCH
        assert ruleset == "global"

    def test_attribution_shows_correct_ruleset(self, multi_ruleset_filter):
        """Rejection reason should show which ruleset caused it."""
        stream = make_stream("Apex Legends", "Apex ranked")
        passed, reason, ruleset = multi_ruleset_filter.apply_filters(stream, ["global", "destiny"])

        assert passed is False
        assert ruleset == "destiny"  # destiny ruleset has the allow list


class TestStreamFilterInvalidRegex:
    """Tests for handling invalid regex patterns."""

    def test_invalid_regex_in_block_is_skipped(self):
        """Invalid regex patterns should be skipped without crashing."""
        config = make_config(
            [
                Ruleset(
                    name="bad",
                    filters=Filters(
                        block_categories=["[invalid regex", "^Valid$"],
                    ),
                )
            ]
        )
        stream_filter = StreamFilter(config)
        stream = make_stream("Valid", "Test")

        # should still match the valid pattern
        passed, reason, ruleset = stream_filter.apply_filters(stream, ["bad"])
        assert passed is False
        assert reason == RejectReason.CATEGORY_BLOCK_MATCH
        assert ruleset == "bad"

    def test_invalid_regex_in_allow_is_skipped(self):
        """Invalid regex in allow list should be skipped."""
        config = make_config(
            [
                Ruleset(
                    name="bad",
                    filters=Filters(
                        allow_categories=["[invalid", "^Good$"],
                    ),
                )
            ]
        )
        stream_filter = StreamFilter(config)
        stream = make_stream("Good", "Test")

        passed, reason, ruleset = stream_filter.apply_filters(stream, ["bad"])
        assert passed is True

    def test_all_invalid_regex_passes_nothing(self):
        """If all patterns are invalid, nothing matches."""
        config = make_config(
            [
                Ruleset(
                    name="bad",
                    filters=Filters(
                        allow_categories=["[invalid1", "[invalid2"],
                    ),
                )
            ]
        )
        stream_filter = StreamFilter(config)
        stream = make_stream("Anything", "Test")

        passed, reason, ruleset = stream_filter.apply_filters(stream, ["bad"])
        # nothing matches the allow list
        assert passed is False


class TestStreamFilterComplexPatterns:
    """Tests for complex regex patterns from real config."""

    @pytest.fixture
    def real_world_filter(self):
        """Filter with patterns similar to production config."""
        config = make_config(
            [
                Ruleset(
                    name="global",
                    filters=Filters(
                        block_categories=[
                            "^(.*?)Minecraft(.*?)$",
                            "^(.*?)Pok[eé]mon(.*?)$",
                            "^Just Chatting$",
                        ],
                        block_titles=[
                            "(?i).*#ad.*",
                            "(?i).*(vod|(?:vod-?|re-?broad)cast)(ing)?.*",
                            "(?i).*(watch ?party).*",
                        ],
                    ),
                ),
            ]
        )
        return StreamFilter(config)

    def test_minecraft_variations_blocked(self, real_world_filter):
        """Various Minecraft categories should be blocked."""
        for category in ["Minecraft", "Minecraft Dungeons", "Minecraft: Story Mode"]:
            stream = make_stream(category, "Playing")
            passed, _, _ = real_world_filter.apply_filters(stream, ["global"])
            assert passed is False, f"{category} should be blocked"

    def test_pokemon_with_accent_blocked(self, real_world_filter):
        """Pokémon with accent should be blocked."""
        stream = make_stream("Pokémon Scarlet/Violet", "Catching them all")
        passed, _, _ = real_world_filter.apply_filters(stream, ["global"])
        assert passed is False

    def test_vod_titles_blocked(self, real_world_filter):
        """VOD/rebroadcast titles should be blocked."""
        for title in ["VOD review", "vodcast", "rebroadcast", "rebroadcasting"]:
            stream = make_stream("Destiny 2", title)
            passed, _, _ = real_world_filter.apply_filters(stream, ["global"])
            assert passed is False, f"'{title}' should be blocked"

    def test_watch_party_blocked(self, real_world_filter):
        """Watch party titles should be blocked."""
        stream = make_stream("Destiny 2", "Watch party for the reveal!")
        passed, _, _ = real_world_filter.apply_filters(stream, ["global"])
        assert passed is False

    def test_normal_stream_passes(self, real_world_filter):
        """Normal streams should pass."""
        stream = make_stream("Destiny 2", "Trials carries - come hang out!")
        passed, reason, ruleset = real_world_filter.apply_filters(stream, ["global"])
        assert passed is True
        assert reason == ""
        assert ruleset == ""
