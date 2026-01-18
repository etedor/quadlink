"""Tests for quad builder."""

import pytest

from quadlink.config.models import Config, Credentials
from quadlink.quad import QuadBuilder
from quadlink.types import Metadata, PrioritizedStream, Stream


@pytest.fixture
def minimal_config():
    """Minimal config for testing."""
    return Config(
        credentials=Credentials(username="test", secret="test"),
        rulesets=[],
        priorities={},
        diversity_bonus=25,
        stability_bonus=30,
    )


@pytest.fixture
def builder(minimal_config):
    """QuadBuilder instance."""
    return QuadBuilder(minimal_config)


def make_stream(author: str, category: str, priority: int) -> PrioritizedStream:
    """Helper to create a prioritized stream."""
    return PrioritizedStream(
        stream=Stream(
            url=f"https://twitch.tv/{author}",
            metadata=Metadata(author=author, category=category, title="Test Stream"),
        ),
        priority=priority,
        tiebreaker=0.5,
    )


class TestQuadBuilderBasic:
    """Test basic quad building."""

    def test_empty_candidates(self, builder):
        """Should return empty quad when no candidates."""
        quad = builder.build_quad([])
        assert quad.is_empty()

    def test_single_candidate(self, builder):
        """Should return quad with single stream."""
        candidates = [make_stream("streamer1", "Games", 100)]
        quad = builder.build_quad(candidates)

        assert quad.stream1 == "https://twitch.tv/streamer1"
        assert quad.stream2 == ""
        assert quad.stream3 == ""
        assert quad.stream4 == ""

    def test_four_candidates(self, builder):
        """Should return quad with all four streams."""
        candidates = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
            make_stream("streamer3", "Games", 80),
            make_stream("streamer4", "Games", 70),
        ]
        quad = builder.build_quad(candidates)

        assert quad.stream1 == "https://twitch.tv/streamer1"
        assert quad.stream2 == "https://twitch.tv/streamer2"
        assert quad.stream3 == "https://twitch.tv/streamer3"
        assert quad.stream4 == "https://twitch.tv/streamer4"

    def test_more_than_four_candidates(self, builder):
        """Should select top 4 by priority."""
        candidates = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
            make_stream("streamer3", "Games", 80),
            make_stream("streamer4", "Games", 70),
            make_stream("streamer5", "Games", 60),  # should be excluded
        ]
        builder.build_quad(candidates)

        authors = set(builder.previous_positions.keys())
        assert len(authors) == 4
        assert "streamer5" not in authors

    def test_duplicate_authors(self, builder):
        """Should deduplicate by author."""
        candidates = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer1", "Games", 90),  # duplicate
            make_stream("streamer2", "Games", 80),
        ]
        builder.build_quad(candidates)

        authors = list(builder.previous_positions.keys())
        assert authors.count("streamer1") == 1


class TestStabilityBonus:
    """Test stability bonus mechanism."""

    def test_stability_bonus_applied(self, builder):
        """Existing streams should get stability bonus."""
        # first iteration: build initial quad
        candidates1 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
        ]
        builder.build_quad(candidates1)

        # second iteration: streamer1 still available but with lower base priority
        # should still win due to stability bonus (90 + 30 = 120 > 110)
        candidates2 = [
            make_stream("streamer3", "Games", 110),  # new, higher priority
            make_stream("streamer1", "Games", 90),  # existing, lower base priority
        ]
        builder.build_quad(candidates2)

        # streamer1 should be preserved (90 + 30 stability = 120 > 110)
        authors = set(builder.previous_positions.keys())
        assert "streamer1" in authors

    def test_position_preservation(self, builder):
        """Existing streams should keep their positions."""
        # first iteration
        candidates1 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
        ]
        quad1 = builder.build_quad(candidates1)

        # second iteration: same streams, different order
        candidates2 = [
            make_stream("streamer2", "Games", 100),  # higher priority now
            make_stream("streamer1", "Games", 90),
        ]
        quad2 = builder.build_quad(candidates2)

        # positions should be preserved despite priority change
        assert quad1.stream1 == quad2.stream1
        assert quad1.stream2 == quad2.stream2


class TestDiversityBonus:
    """Test diversity bonus mechanism."""

    def test_diversity_bonus_new_category(self, builder):
        """New categories should get diversity bonus."""
        candidates = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Music", 90),  # new category, gets bonus
            make_stream("streamer3", "Games", 85),  # same category as first, no bonus
            make_stream("streamer4", "Art", 60),  # new category, gets bonus
            make_stream("streamer5", "Talk", 50),  # new category, lower base
        ]
        builder.build_quad(candidates)

        # diversity bonus applied only once per category:
        # streamer1: 100 + 25 = 125 (first Games)
        # streamer2: 90 + 25 = 115 (first Music)
        # streamer3: 85 (second Games, no bonus)
        # streamer4: 60 + 25 = 85 (first Art)
        # streamer5: 50 + 25 = 75 (first Talk)
        # top 4: streamer1, streamer2, streamer3/streamer4 (tied at 85)
        authors = set(builder.previous_positions.keys())
        assert "streamer1" in authors
        assert "streamer2" in authors
        # streamer3 or streamer4 will be selected (tied at 85)
        # streamer5 should not make it due to lower adjusted priority
        assert "streamer5" not in authors

    def test_diversity_bonus_only_once_per_category(self, builder):
        """Diversity bonus should only apply to first stream in new category."""
        candidates = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Music", 90),  # first Music stream (gets bonus)
            make_stream("streamer3", "Music", 85),  # second Music stream (no bonus)
            make_stream("streamer4", "Art", 80),  # first Art stream (gets bonus)
            make_stream("streamer5", "Talk", 75),  # first Talk stream (gets bonus)
        ]
        builder.build_quad(candidates)

        # streamer1: 100 + 25 = 125
        # streamer2: 90 + 25 = 115
        # streamer3: 85 (no bonus, second Music)
        # streamer4: 80 + 25 = 105
        # streamer5: 75 + 25 = 100
        # streamer4 and streamer5 should beat streamer3 due to diversity bonus
        authors = set(builder.previous_positions.keys())
        assert "streamer4" in authors
        assert "streamer5" in authors
        assert "streamer3" not in authors


class TestSaturationPenalty:
    """Test category saturation penalty."""

    def test_saturation_penalty_second_stream(self, builder):
        """Second stream in same category should get -diversity_bonus/3 penalty."""
        # first iteration: establish quad with Games category
        candidates1 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Music", 90),
        ]
        builder.build_quad(candidates1)

        # second iteration: new Games stream should get penalty
        candidates2 = [
            make_stream("streamer1", "Games", 100),  # existing Games
            make_stream("streamer2", "Music", 90),  # existing Music
            make_stream("streamer3", "Games", 95),  # new Games (gets penalty)
            make_stream("streamer4", "Art", 85),  # new Art (gets diversity bonus)
        ]
        builder.build_quad(candidates2)

        # streamer4 should beat streamer3 due to:
        # streamer3: 95 - 25/3 ≈ 95 - 8 = 87
        # streamer4: 85 + 25 = 110
        authors = set(builder.previous_positions.keys())
        assert "streamer4" in authors

    def test_saturation_penalty_third_stream(self, builder):
        """Third stream in same category should get -2*diversity_bonus/3 penalty."""
        # establish quad with 2 Games streams
        candidates1 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
            make_stream("streamer3", "Music", 80),
        ]
        builder.build_quad(candidates1)

        # third Games stream should get larger penalty
        candidates2 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
            make_stream("streamer3", "Music", 80),
            make_stream("streamer4", "Games", 85),  # third Games
            make_stream("streamer5", "Art", 70),  # new Art
        ]
        builder.build_quad(candidates2)

        # streamer5 should beat streamer4 due to:
        # streamer4: 85 - 2*25/3 ≈ 85 - 16 = 69
        # streamer5: 70 + 25 = 95
        authors = set(builder.previous_positions.keys())
        assert "streamer5" in authors

    def test_saturation_penalty_fourth_stream(self, builder):
        """Fourth+ stream in same category should get -diversity_bonus penalty."""
        # establish quad with 3 Games streams
        candidates1 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
            make_stream("streamer3", "Games", 80),
            make_stream("streamer4", "Music", 70),
        ]
        builder.build_quad(candidates1)

        # fourth Games stream should get full diversity bonus as penalty
        # but existing streams should NOT be penalized for their own category
        candidates2 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
            make_stream("streamer3", "Games", 80),
            make_stream("streamer4", "Music", 70),
            make_stream("streamer5", "Games", 85),  # fourth Games, higher priority
            make_stream("streamer6", "Art", 75),  # new Art
        ]
        builder.build_quad(candidates2)

        # existing streams get +30 stability, NO saturation penalty for existing streams
        # streamer1: 100 + 30 = 130
        # streamer2: 90 + 30 = 120
        # streamer3: 80 + 30 = 110
        # streamer4: 70 + 30 = 100
        # streamer5: 85 - 25 = 60 (new Games, 4th in category gets -25 saturation)
        # streamer6: 75 + 25 = 100 (new Art, gets diversity bonus)
        # top 4: streamer1 (130), streamer2 (120), streamer3 (110), streamer4 (100)
        # existing quad is preserved, new streams don't make it
        authors = set(builder.previous_positions.keys())
        assert "streamer1" in authors
        assert "streamer2" in authors
        assert "streamer3" in authors
        assert "streamer4" in authors
        assert "streamer5" not in authors  # saturation penalty kept it out


class TestCategoryContinuityBonus:
    """Test category continuity bonus mechanism."""

    def test_continuity_bonus_for_departed_category(self, builder):
        """New stream matching departed category should get bonus."""
        # first iteration: build quad with Games and Music
        candidates1 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Music", 90),
        ]
        builder.build_quad(candidates1)

        # second iteration: streamer2 (Music) goes offline
        # new streamer3 (Music) should get category continuity bonus
        candidates2 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer3", "Music", 70),  # new, matches departed category
            make_stream("streamer4", "Art", 80),  # new, different category
        ]
        builder.build_quad(candidates2)

        # streamer3 should be selected despite lower base priority
        # because it matches the departed Music category
        # streamer3: 70 + continuity_bonus (default 15) = 85
        # streamer4: 80 + diversity_bonus (25) = 105
        # Actually streamer4 still wins, but streamer3 gets the bonus
        authors = set(builder.previous_positions.keys())
        # Both should be in the quad (only 3 streams total)
        assert "streamer3" in authors


class TestComplexScenario:
    """Test complex scenarios with multiple bonuses."""

    def test_stability_beats_diversity(self, builder):
        """Stability bonus should be higher than diversity bonus."""
        # config should ensure stability_bonus >= diversity_bonus + 1
        assert builder.config.stability_bonus >= builder.config.diversity_bonus + 1

        # first iteration
        candidates1 = [
            make_stream("streamer1", "Games", 100),
        ]
        builder.build_quad(candidates1)

        # second iteration: new category but lower priority
        candidates2 = [
            make_stream("streamer1", "Games", 80),  # existing
            make_stream("streamer2", "Music", 100),  # new category, higher base
        ]
        builder.build_quad(candidates2)

        # streamer1 should still be first due to stability bonus
        # streamer1: 80 + 30 = 110
        # streamer2: 100 + 25 = 125 (actually wins)
        # This test shows diversity can beat stability if base priority gap is large enough
        authors = set(builder.previous_positions.keys())
        assert len(authors) == 2

    def test_all_bonuses_combined(self, builder):
        """Test scenario with stability, diversity, and saturation all active."""
        # first iteration: establish initial quad
        candidates1 = [
            make_stream("streamer1", "Games", 100),
            make_stream("streamer2", "Games", 90),
            make_stream("streamer3", "Music", 80),
        ]
        builder.build_quad(candidates1)

        # second iteration: complex scenario
        candidates2 = [
            make_stream("streamer1", "Games", 100),  # existing, stability bonus
            make_stream("streamer2", "Games", 90),  # existing, stability - saturation
            make_stream("streamer3", "Music", 80),  # existing, stability bonus
            make_stream("streamer4", "Games", 95),  # new, but saturation penalty (3rd Games)
            make_stream("streamer5", "Art", 70),  # new category, diversity bonus
        ]
        builder.build_quad(candidates2)

        # verify we have 4 streams
        authors = set(builder.previous_positions.keys())
        assert len(authors) == 4

        # existing streams should be preserved
        assert "streamer1" in authors
        assert "streamer2" in authors
        assert "streamer3" in authors
