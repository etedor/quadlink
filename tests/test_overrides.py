"""Tests for tile holds: drops, put back, unpin, expiry and added channels."""

import pytest

from quadlink.config.models import Config, Credentials
from quadlink.overrides import (
    GRACE_REFRESHES,
    OverrideError,
    Result,
    add_extra,
    age,
    drop_unconfigured,
    place,
    remove_extra,
    swap,
    unpin,
    unpin_all,
)
from quadlink.quad import Hold, Overrides, QuadState, compose
from quadlink.types import Metadata, PrioritizedStream, Stream


def make_config(**bonuses: int) -> Config:
    return Config(
        credentials=Credentials(username="test", secret="test"),
        rulesets=[],
        priorities={},
        **bonuses,
    )


def stream(channel: str, priority: int, category: str = "Games") -> PrioritizedStream:
    return PrioritizedStream(
        stream=Stream(
            url=f"https://twitch.tv/{channel}",
            metadata=Metadata(author=channel.upper(), category=category, title="t"),
        ),
        priority=priority,
        # equal priorities rank alphabetically
        tiebreaker=1 / (1 + ord(channel[0])),
    )


class Scene:
    """Live channels and the daemon's cycle: refreshes age overrides, then compose.

    Every compose is what the daemon's recompose does after a refresh or an action.
    """

    def __init__(self, config: Config, live: dict[str, int], configured: set[str]):
        self.config = config
        self.live = dict(live)
        self.configured = set(configured)
        self.state = QuadState()
        self.overrides = Overrides()

    def pool(self) -> list[PrioritizedStream]:
        pool = [stream(c, p) for c, p in self.live.items()]
        return sorted(pool, key=lambda s: (s.priority, s.tiebreaker), reverse=True)

    def compose(self) -> tuple[str, ...]:
        self.state = compose(self.pool(), self.state, self.overrides, self.config)
        return self.state.channels

    def refresh(self) -> tuple[str, ...]:
        self.overrides, self.state = drop_unconfigured(self.overrides, self.state, self.configured)
        self.overrides, self.state = age(self.overrides, self.state, set(self.live))
        return self.compose()

    def act(self, result: Result) -> tuple[str, ...]:
        self.overrides, self.state = result
        return self.compose()

    def drop(self, channel: str, slot: int) -> tuple[str, ...]:
        return self.act(place(self.overrides, self.state, channel, slot, self.configured))


SIX = {"a": 100, "b": 90, "c": 80, "d": 70, "e": 60, "f": 50}
CONFIGURED = {*SIX, "g", "h"}


@pytest.fixture
def scene() -> Scene:
    """One category, stability bonus 30, A-D on screen."""
    config = make_config(diversity_bonus=0, stability_bonus=30, category_continuity_bonus=0)
    scene = Scene(config, SIX, CONFIGURED)
    assert scene.refresh() == ("a", "b", "c", "d")
    return scene


class TestDrop:
    def test_a_drop_holds_the_tile_and_remembers_what_it_showed(self, scene):
        assert scene.drop("f", 3) == ("a", "b", "c", "f")
        assert scene.overrides.holds == {3: Hold("f", "d")}

    def test_a_replaced_stream_takes_no_other_tile(self, scene):
        """A drop says the tile's stream goes: the top channel does not just move."""
        assert scene.drop("f", 0) == ("f", "b", "c", "d")
        for _ in range(3):
            assert scene.refresh() == ("f", "b", "c", "d")

    def test_a_drop_on_a_held_tile_keeps_the_original_and_frees_the_holder(self, scene):
        scene.drop("f", 3)
        assert scene.drop("e", 3) == ("a", "b", "c", "e")
        assert scene.overrides.holds == {3: Hold("e", "d")}
        assert scene.refresh() == ("a", "b", "c", "e")

    def test_a_drop_on_a_hold_that_remembers_nothing_remembers_the_holder(self, scene):
        scene.drop("f", 3)
        del scene.live["d"]
        for _ in range(GRACE_REFRESHES + 1):
            scene.refresh()
        assert scene.overrides.holds == {3: Hold("f")}
        assert scene.drop("e", 3) == ("a", "b", "c", "e")
        assert scene.overrides.holds == {3: Hold("e", "f")}

    def test_dropping_a_shown_stream_on_another_tile_swaps_them(self, scene):
        assert scene.drop("a", 3) == ("d", "b", "c", "a")
        assert scene.overrides.holds == {}

    def test_dropping_a_stream_on_its_own_tile_changes_nothing(self, scene):
        before = (scene.overrides, scene.state)
        assert scene.drop("c", 2) == ("a", "b", "c", "d")
        assert (scene.overrides, scene.state) == before

    def test_a_replaced_stream_dropped_elsewhere_is_forgotten(self, scene):
        scene.drop("f", 3)
        assert scene.drop("d", 0) == ("d", "b", "c", "f")
        assert scene.overrides.holds == {0: Hold("d", "a"), 3: Hold("f")}

    def test_a_holder_out_of_the_pool_dropped_elsewhere_hands_its_old_tile_back(self, scene):
        scene.drop("f", 3)
        del scene.live["f"]
        assert scene.refresh() == ("a", "b", "c", "e")
        scene.live["f"] = 50
        overrides, state = place(scene.overrides, scene.state, "f", 1, CONFIGURED)
        assert overrides.holds == {1: Hold("f", "b")}
        assert state.channels[3] == "d"

    def test_rejects_an_unknown_channel_and_a_bad_slot(self, scene):
        with pytest.raises(OverrideError):
            place(scene.overrides, scene.state, "zz", 0, CONFIGURED)
        with pytest.raises(OverrideError):
            place(scene.overrides, scene.state, "e", 4, CONFIGURED)


class TestUndo:
    def test_unpin_gives_the_tile_back_to_the_replaced(self, scene):
        scene.drop("f", 3)
        assert scene.act(unpin(scene.overrides, scene.state, 3)) == ("a", "b", "c", "d")
        assert scene.overrides.holds == {}

    def test_unpin_after_repeated_drops_restores_the_original(self, scene):
        scene.drop("f", 3)
        scene.drop("e", 3)
        assert scene.act(unpin(scene.overrides, scene.state, 3)) == ("a", "b", "c", "d")
        # E and F go back to selection, which did not choose them before
        assert scene.refresh() == ("a", "b", "c", "d")

    def test_unpin_of_a_tile_with_no_hold_changes_nothing(self, scene):
        assert unpin(scene.overrides, scene.state, 2) == (scene.overrides, scene.state)

    def test_unpin_all_hands_every_tile_back(self, scene):
        scene.drop("f", 3)
        scene.drop("e", 0)
        assert scene.act(unpin_all(scene.overrides, scene.state)) == ("a", "b", "c", "d")
        assert scene.overrides.holds == {}

    def test_unpin_of_a_tile_whose_replaced_is_offline_leaves_it_to_selection(self, scene):
        scene.drop("f", 3)
        del scene.live["d"]
        scene.refresh()
        assert scene.act(unpin(scene.overrides, scene.state, 3)) == ("a", "b", "c", "e")


class TestSwap:
    def test_unheld_channels_swap_slots(self, scene):
        assert scene.act(swap(scene.overrides, scene.state, 0, 3)) == ("d", "b", "c", "a")

    def test_a_hold_moves_with_its_holder(self, scene):
        scene.drop("f", 3)
        assert scene.act(swap(scene.overrides, scene.state, 3, 0)) == ("f", "b", "c", "a")
        assert scene.overrides.holds == {0: Hold("f", "d")}
        # put back still restores the tile the hold now sits on
        assert scene.act(unpin(scene.overrides, scene.state, 0)) == ("d", "b", "c", "a")

    def test_same_slot_changes_nothing(self, scene):
        assert swap(scene.overrides, scene.state, 1, 1) == (scene.overrides, scene.state)


class TestGrace:
    def test_grace_is_two_refreshes(self):
        assert GRACE_REFRESHES == 2

    def test_a_holder_out_of_the_pool_keeps_its_tile_through_the_grace(self, scene):
        scene.drop("f", 3)
        del scene.live["f"]
        for absent in range(1, GRACE_REFRESHES + 1):
            # selection fills the tile meanwhile; D stays listed
            assert scene.refresh() == ("a", "b", "c", "e")
            assert scene.overrides.absent == {"f": absent}
        # after the grace the hold ends as Unpin ends it
        assert scene.refresh() == ("a", "b", "c", "d")
        assert scene.overrides.holds == {}

    def test_a_holder_back_within_the_grace_reclaims_its_tile(self, scene):
        scene.drop("f", 3)
        del scene.live["f"]
        scene.refresh()
        scene.live["f"] = 50
        assert scene.refresh() == ("a", "b", "c", "f")
        assert scene.overrides.absent == {}

    def test_a_replaced_stream_offline_past_the_grace_is_forgotten(self, scene):
        scene.drop("f", 3)
        del scene.live["d"]
        for _ in range(GRACE_REFRESHES):
            scene.refresh()
        assert scene.overrides.holds == {3: Hold("f", "d")}
        scene.refresh()
        # the hold stays; when it ends, selection fills the tile
        assert scene.overrides.holds == {3: Hold("f")}
        assert scene.overrides.absent == {}

    def test_a_replaced_stream_back_within_the_grace_keeps_its_claim(self, scene):
        scene.drop("f", 3)
        del scene.live["d"]
        scene.refresh()
        scene.live["d"] = 50
        for _ in range(GRACE_REFRESHES + 1):
            scene.refresh()
        assert scene.overrides.holds == {3: Hold("f", "d")}


class TestExtras:
    """Channels added beside the config reach the quad only as holders."""

    def added(self, scene: Scene) -> None:
        # the daemon counts added channels as configured
        scene.configured |= {"xy"}
        scene.overrides = add_extra(scene.overrides, "xy", CONFIGURED)
        scene.live["xy"] = 500

    def test_an_added_channel_stays_off_screen_until_dropped(self, scene):
        self.added(scene)
        assert scene.refresh() == ("a", "b", "c", "d")
        assert scene.drop("xy", 1) == ("a", "xy", "c", "d")

    def test_an_added_holder_replaced_by_a_drop_is_not_remembered(self, scene):
        self.added(scene)
        scene.drop("xy", 3)
        assert scene.drop("e", 3) == ("a", "b", "c", "e")
        assert scene.overrides.holds == {3: Hold("e", "d")}

    def test_remove_ends_its_hold_as_unpin(self, scene):
        self.added(scene)
        scene.drop("xy", 3)
        result = remove_extra(scene.overrides, scene.state, "xy")
        # the daemon stops looking it up
        del scene.live["xy"]
        assert scene.act(result) == ("a", "b", "c", "d")
        assert scene.overrides.extras == frozenset()

    def test_unpin_all_keeps_added_channels(self, scene):
        self.added(scene)
        overrides, _ = unpin_all(scene.overrides, scene.state)
        assert overrides.extras == {"xy"}

    def test_add_rejects_bad_names_and_listed_channels(self, scene):
        with pytest.raises(OverrideError):
            add_extra(scene.overrides, "not a name", CONFIGURED)
        with pytest.raises(OverrideError):
            add_extra(scene.overrides, "a", CONFIGURED)
        once = add_extra(scene.overrides, "xy", CONFIGURED)
        with pytest.raises(OverrideError):
            add_extra(once, "xy", CONFIGURED)
        with pytest.raises(OverrideError):
            remove_extra(scene.overrides, scene.state, "xy")


class TestUnconfigured:
    def test_a_holder_removed_from_the_config_hands_its_tile_back(self, scene):
        scene.drop("f", 3)
        scene.configured -= {"f"}
        assert scene.refresh() == ("a", "b", "c", "d")
        assert scene.overrides.holds == {}

    def test_a_replaced_channel_removed_from_the_config_is_forgotten(self, scene):
        scene.drop("f", 3)
        scene.configured -= {"d"}
        scene.refresh()
        assert scene.overrides.holds == {3: Hold("f")}


class TestCompose:
    """Holds in compose; natural selection itself is covered by test_quad.py."""

    def test_a_holder_is_never_displaced(self, scene):
        scene.drop("f", 3)
        scene.live["g"] = 500
        # the lowest unheld channel, C at 80 + 30, gives way
        assert scene.refresh() == ("a", "b", "g", "f")

    def test_holds_for_channels_out_of_the_pool_change_nothing(self, scene):
        natural = compose(scene.pool(), scene.state, Overrides(), scene.config)
        overrides = Overrides(holds={0: Hold("g", "h")})
        assert compose(scene.pool(), scene.state, overrides, scene.config) == natural

    def test_a_holder_counts_for_category_saturation(self):
        config = make_config(diversity_bonus=25, stability_bonus=30, category_continuity_bonus=0)
        pool = [
            stream("q", 100, "Games"),
            stream("w", 95, "Art"),
            stream("r", 80, "Chess"),
            stream("u", 80, "Music"),
            stream("x", 10, "Games"),
        ]
        natural = compose(pool, QuadState(), Overrides(), config)
        assert set(natural.channels) == {"q", "w", "r", "u"}
        state = compose(pool, QuadState(), Overrides(holds={0: Hold("x")}), config)
        assert state.channels[0] == "x"
        assert set(state.channels[1:]) == {"w", "r", "u"}


class TestInvariants:
    def test_a_channel_holds_at_most_one_tile(self):
        with pytest.raises(ValueError):
            Overrides(holds={0: Hold("a"), 1: Hold("a")})

    def test_a_channel_is_listed_at_most_once(self):
        with pytest.raises(ValueError):
            Overrides(holds={0: Hold("a", "c"), 1: Hold("b", "c")})

    def test_a_holder_is_not_listed(self):
        with pytest.raises(ValueError):
            Overrides(holds={0: Hold("a", "b"), 1: Hold("b")})

    def test_added_channels_are_never_listed(self):
        with pytest.raises(ValueError):
            Overrides(holds={0: Hold("a", "x")}, extras=frozenset({"x"}))

    def test_slots_are_0_to_3(self):
        with pytest.raises(ValueError):
            Overrides(holds={4: Hold("a")})
