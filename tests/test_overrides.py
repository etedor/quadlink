"""Tests for pins, benches, operator actions and their expiry."""

import pytest

from quadlink.config.models import Config, Credentials
from quadlink.overrides import (
    GRACE_REFRESHES,
    OverrideError,
    age,
    bench,
    drop_unconfigured,
    pin,
    place,
    reset,
    swap,
    unbench,
    unpin,
)
from quadlink.quad import Overrides, QuadState, compose, waiting
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
        self.configured = configured
        self.categories: dict[str, str] = {}
        self.state = QuadState()
        self.overrides = Overrides()

    def pool(self) -> list[PrioritizedStream]:
        pool = [stream(c, p, self.categories.get(c, "Games")) for c, p in self.live.items()]
        return sorted(pool, key=lambda s: (s.priority, s.tiebreaker), reverse=True)

    def compose(self) -> tuple[str, ...]:
        self.state = compose(self.pool(), self.state, self.overrides, self.config)
        return self.state.channels

    def refresh(self) -> tuple[str, ...]:
        self.overrides = drop_unconfigured(self.overrides, self.configured)
        self.overrides = age(self.overrides, set(self.live))
        return self.compose()

    def act(self, overrides: Overrides) -> tuple[str, ...]:
        self.overrides = overrides
        return self.compose()

    def waiting(self) -> list[str]:
        return waiting(self.pool(), self.state, self.overrides, self.config)


SIX = {"a": 100, "b": 90, "c": 80, "d": 70, "e": 60, "f": 50}
CONFIGURED = {*SIX, "g", "h"}


@pytest.fixture
def scene() -> Scene:
    """The spec's scenario: one category, stability bonus 30, A-D on screen."""
    # with one category the diversity bonus still penalizes newcomers through
    # saturation, so "category bonuses drop out" needs them at zero
    config = make_config(diversity_bonus=0, stability_bonus=30, category_continuity_bonus=0)
    scene = Scene(config, SIX, CONFIGURED)
    assert scene.refresh() == ("a", "b", "c", "d")
    return scene


class TestScenario:
    """docs/superpowers/specs/2026-10-22-pins-and-benches.md, Scenario, step by step."""

    def step1(self, scene: Scene) -> None:
        quad = scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        assert quad == ("a", "b", "c", "f")
        assert scene.overrides.pins == {"f": 3}
        assert scene.overrides.benches == {"d"}

    def step2(self, scene: Scene) -> None:
        scene.live["g"] = 120
        assert scene.refresh() == ("a", "b", "g", "f")

    def step3(self, scene: Scene) -> None:
        del scene.live["f"]
        assert scene.refresh() == ("a", "b", "g", "c")
        assert scene.overrides.pins == {"f": 3}

    def test_main_line(self, scene):
        self.step1(scene)
        self.step2(scene)
        self.step3(scene)

        # 4a: F is back within the grace and reclaims slot 3; C leaves again
        scene.live["f"] = 50
        assert scene.refresh() == ("a", "b", "g", "f")
        assert scene.overrides.absent == {}

        # 5: D goes offline; its bench clears after the grace, with no quad change
        del scene.live["d"]
        for _ in range(GRACE_REFRESHES + 1):
            assert scene.refresh() == ("a", "b", "g", "f")
        assert scene.overrides.benches == frozenset()
        scene.live["d"] = 70
        assert scene.refresh() == ("a", "b", "g", "f")

        # 6: drag tile 0 (A) onto tile 3 (F): F's pin moves with it
        overrides, scene.state = swap(scene.overrides, scene.state, 0, 3)
        assert scene.act(overrides) == ("f", "b", "g", "a")
        assert scene.overrides.pins == {"f": 0}

        # 7: pin the other three; the quad is frozen and a new 200 waits
        for slot in (1, 2, 3):
            scene.act(pin(scene.overrides, scene.state, slot))
        assert scene.overrides.pins == {"f": 0, "b": 1, "g": 2, "a": 3}
        scene.live["h"] = 200
        assert scene.refresh() == ("f", "b", "g", "a")
        assert scene.waiting() == ["h"]

    def test_step2_prime_stability_protects_the_lowest_unpinned(self, scene):
        self.step1(scene)
        scene.live["g"] = 95
        assert scene.refresh() == ("a", "b", "c", "f")

    def test_step4b_pin_clears_after_the_grace(self, scene):
        self.step1(scene)
        self.step2(scene)
        self.step3(scene)
        for _ in range(GRACE_REFRESHES - 1):
            assert scene.refresh() == ("a", "b", "g", "c")
            assert scene.overrides.pins == {"f": 3}
        assert scene.refresh() == ("a", "b", "g", "c")
        assert scene.overrides.pins == {}
        # F returns and competes at 50
        scene.live["f"] = 50
        assert scene.refresh() == ("a", "b", "g", "c")


class TestCompose:
    """Overrides in compose; natural selection itself is covered by test_quad.py."""

    def test_benched_channel_leaves_and_its_slot_refills(self, scene):
        assert scene.act(bench(scene.overrides, "b", CONFIGURED)) == ("a", "e", "c", "d")

    def test_benches_leaving_fewer_than_four_leave_slots_empty(self, scene):
        scene.live = {"a": 100, "b": 90, "c": 80}
        scene.refresh()
        assert scene.act(bench(scene.overrides, "b", CONFIGURED)) == ("a", "", "c", "")

    def test_benching_everyone_empties_the_quad(self, scene):
        scene.live = {"a": 100}
        scene.refresh()
        assert scene.act(bench(scene.overrides, "a", CONFIGURED)) == ("", "", "", "")

    def test_pinned_channel_is_never_displaced(self, scene):
        scene.act(pin(scene.overrides, scene.state, 3))
        scene.live["g"] = 500
        # the lowest unpinned channel, C at 80 + 30, gives way
        assert scene.refresh() == ("a", "b", "g", "d")

    def test_occupant_of_a_reclaimed_slot_competes_as_a_newcomer(self, scene):
        probe = Scene(
            scene.config, {"a": 100, "b": 90, "x": 75, "y": 55}, {"a", "b", "x", "y", "c"}
        )
        assert probe.refresh() == ("a", "b", "x", "y")
        probe.act(pin(probe.overrides, probe.state, 3))
        probe.live["c"] = 80
        assert probe.refresh() == ("a", "b", "x", "y")
        del probe.live["y"]
        assert probe.refresh() == ("a", "b", "x", "c")
        # C (80, no stability bonus) does not outrank X (75 + 30), on screen all along
        probe.live["y"] = 55
        assert probe.refresh() == ("a", "b", "x", "y")

    def test_overrides_for_channels_out_of_the_pool_change_nothing(self, scene):
        natural = compose(scene.pool(), scene.state, Overrides(), scene.config)
        overrides = Overrides(pins={"g": 0}, benches=frozenset({"h"}))
        assert compose(scene.pool(), scene.state, overrides, scene.config) == natural

    def test_pinned_channel_counts_for_category_saturation(self):
        config = make_config(diversity_bonus=25, stability_bonus=30, category_continuity_bonus=0)
        pool = [
            stream("q", 100, "Games"),
            stream("w", 95, "Art"),
            stream("r", 80, "Chess"),
            stream("u", 80, "Music"),
            stream("x", 10, "Games"),
        ]
        # without the pin Q gets the Games diversity bonus (125) and is selected
        natural = compose(pool, QuadState(), Overrides(), config)
        assert set(natural.channels) == {"q", "w", "r", "u"}
        # with X pinned, Games is present, so Q gets a saturation penalty (92)
        state = compose(pool, QuadState(), Overrides(pins={"x": 0}), config)
        assert state.channels[0] == "x"
        assert set(state.channels[1:]) == {"w", "r", "u"}

    def test_pinned_channel_keeps_its_category(self):
        config = make_config()
        pool = [stream("a", 100, "Games"), stream("x", 10, "Chess")]
        state = compose(pool, QuadState(), Overrides(pins={"x": 2}), config)
        assert state.channels == ("a", "", "x", "")
        assert state.categories == {"a": "Games", "x": "Chess"}

    def test_pin_moves_an_on_screen_channel_and_frees_its_slot(self, scene):
        overrides = place(scene.overrides, scene.state, "a", 3, CONFIGURED)
        assert scene.act(overrides) == ("e", "b", "c", "a")
        assert scene.overrides.benches == {"d"}

    def test_waiting_is_empty_without_pins(self, scene):
        scene.live["g"] = 500
        scene.refresh()
        assert scene.waiting() == []


class TestPlace:
    def test_onto_an_empty_slot_pins(self, scene):
        scene.live = {"a": 100}
        scene.refresh()
        overrides = place(scene.overrides, scene.state, "b", 2, CONFIGURED)
        assert overrides == Overrides(pins={"b": 2})

    def test_onto_a_pinned_occupant_unpins_and_benches_it(self, scene):
        scene.act(pin(scene.overrides, scene.state, 1))
        overrides = place(scene.overrides, scene.state, "e", 1, CONFIGURED)
        assert overrides.pins == {"e": 1}
        assert overrides.benches == {"b"}

    def test_a_benched_channel_is_unbenched_and_pinned(self, scene):
        scene.act(bench(scene.overrides, "e", CONFIGURED))
        overrides = place(scene.overrides, scene.state, "e", 0, CONFIGURED)
        assert overrides.pins == {"e": 0}
        assert overrides.benches == {"a"}
        assert scene.act(overrides) == ("e", "b", "c", "d")

    def test_onto_its_own_slot_only_pins(self, scene):
        overrides = place(scene.overrides, scene.state, "c", 2, CONFIGURED)
        assert overrides == Overrides(pins={"c": 2})

    def test_a_pinned_channel_moves_its_pin(self, scene):
        scene.act(pin(scene.overrides, scene.state, 0))
        overrides = place(scene.overrides, scene.state, "a", 3, CONFIGURED)
        assert overrides.pins == {"a": 3}

    def test_replaces_the_pin_held_for_a_channel_out_of_the_pool(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        del scene.live["f"]
        assert scene.refresh() == ("a", "b", "c", "e")
        overrides = place(scene.overrides, scene.state, "d", 3, CONFIGURED)
        assert overrides.pins == {"d": 3}
        assert overrides.benches == {"e"}
        assert overrides.absent == {}

    def test_rejects_an_unknown_channel_and_a_bad_slot(self, scene):
        with pytest.raises(OverrideError, match="'zz' is not in the config"):
            place(scene.overrides, scene.state, "zz", 0, CONFIGURED)
        for slot in (-1, 4, True):
            with pytest.raises(OverrideError, match="use a slot from 0 to 3"):
                place(scene.overrides, scene.state, "a", slot, CONFIGURED)


class TestSwap:
    def test_unpinned_channels_swap_slots(self, scene):
        overrides, scene.state = swap(scene.overrides, scene.state, 1, 2)
        assert overrides == scene.overrides
        assert scene.state.quad.slots[1] == "https://twitch.tv/c"
        assert scene.act(overrides) == ("a", "c", "b", "d")

    def test_with_an_empty_slot_moves_the_channel(self, scene):
        scene.live = {"a": 100, "b": 90}
        scene.refresh()
        overrides, scene.state = swap(scene.overrides, scene.state, 0, 3)
        assert scene.act(overrides) == ("", "b", "", "a")

    def test_both_pins_move(self, scene):
        scene.act(pin(scene.overrides, scene.state, 0))
        scene.act(pin(scene.overrides, scene.state, 1))
        overrides, scene.state = swap(scene.overrides, scene.state, 0, 1)
        assert overrides.pins == {"a": 1, "b": 0}
        assert scene.act(overrides) == ("b", "a", "c", "d")

    def test_a_pin_moving_in_replaces_a_pin_held_for_a_channel_out_of_the_pool(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        scene.act(pin(scene.overrides, scene.state, 0))
        del scene.live["f"]
        assert scene.refresh() == ("a", "b", "c", "e")
        overrides, scene.state = swap(scene.overrides, scene.state, 0, 3)
        assert overrides.pins == {"a": 3}

    def test_same_slot_changes_nothing(self, scene):
        assert swap(scene.overrides, scene.state, 2, 2) == (scene.overrides, scene.state)

    def test_rejects_a_bad_slot(self, scene):
        with pytest.raises(OverrideError):
            swap(scene.overrides, scene.state, 0, 7)


class TestPinUnpin:
    def test_pin_holds_the_channel_in_its_current_slot(self, scene):
        assert pin(scene.overrides, scene.state, 2) == Overrides(pins={"c": 2})

    def test_pin_of_an_empty_slot_is_an_error(self, scene):
        scene.live = {"a": 100}
        scene.refresh()
        with pytest.raises(OverrideError, match="slot 1 is empty"):
            pin(scene.overrides, scene.state, 1)

    def test_unpin_keeps_the_channel_until_selection_displaces_it(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        assert scene.act(unpin(scene.overrides, 3)) == ("a", "b", "c", "f")
        assert scene.overrides.pins == {}
        # unpinned, F holds its slot by priority alone: G (90) outranks F (50 + 30)
        scene.live["g"] = 90
        assert scene.refresh() == ("a", "b", "c", "g")

    def test_unpin_of_an_unpinned_slot_changes_nothing(self, scene):
        assert unpin(scene.overrides, 0) == scene.overrides


class TestBench:
    def test_bench_removes_the_pin(self, scene):
        scene.act(pin(scene.overrides, scene.state, 0))
        overrides = bench(scene.overrides, "a", CONFIGURED)
        assert overrides == Overrides(benches=frozenset({"a"}))

    def test_unbench_returns_the_channel_by_priority(self, scene):
        scene.act(bench(scene.overrides, "a", CONFIGURED))
        assert scene.state.channels == ("e", "b", "c", "d")
        # back at 100, A displaces the lowest on screen, E at 60 + 30
        assert scene.act(unbench(scene.overrides, "a", CONFIGURED)) == ("a", "b", "c", "d")

    def test_rejects_an_unknown_channel(self, scene):
        with pytest.raises(OverrideError):
            bench(scene.overrides, "zz", CONFIGURED)
        with pytest.raises(OverrideError):
            unbench(scene.overrides, "zz", CONFIGURED)

    def test_reset_clears_everything_and_keeps_staying_slots(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        scene.act(bench(scene.overrides, "a", CONFIGURED))
        assert scene.state.channels == ("e", "b", "c", "f")
        # A (100) outranks F (50 + 30); the channels that stay keep their slots
        assert scene.act(reset()) == ("e", "b", "c", "a")
        assert scene.overrides == Overrides()


class TestInvariants:
    def test_a_channel_is_never_both_pinned_and_benched(self):
        with pytest.raises(ValueError, match="both pinned and benched"):
            Overrides(pins={"a": 0}, benches=frozenset({"a"}))

    def test_at_most_one_pin_per_slot(self):
        with pytest.raises(ValueError, match="more than one pin per slot"):
            Overrides(pins={"a": 0, "b": 0})

    def test_pin_slots_are_0_to_3(self):
        with pytest.raises(ValueError, match="out of range"):
            Overrides(pins={"a": 4})

    def test_actions_keep_the_invariants(self, scene):
        """Overrides() raises on a broken invariant, so each step checks them."""
        o = scene.overrides
        o = place(o, scene.state, "e", 0, CONFIGURED)
        # benching E ends its pin, and with it the bench its placement put on A
        o = bench(o, "e", CONFIGURED)
        assert (o.pins, o.benches) == ({}, {"e"})
        o = place(o, scene.state, "e", 1, CONFIGURED)
        assert (o.pins, o.benches, o.bound) == ({"e": 1}, {"b"}, {"b": "e"})
        # the state still shows B in slot 1: pinning it replaces E's pin and unbenches B
        o = pin(o, scene.state, 1)
        assert (o.pins, o.benches, o.bound) == ({"b": 1}, set(), {})


class TestGrace:
    def test_grace_is_two_refreshes(self):
        assert GRACE_REFRESHES == 2

    def test_pin_and_bench_survive_the_grace_and_clear_after(self):
        overrides = Overrides(pins={"f": 3}, benches=frozenset({"d"}))
        for absent in range(1, GRACE_REFRESHES + 1):
            overrides = age(overrides, pool={"a"})
            assert overrides.pins == {"f": 3}
            assert overrides.absent == {"f": absent, "d": absent}
        assert age(overrides, pool={"a"}) == Overrides()

    def test_returning_starts_the_count_over(self):
        overrides = Overrides(pins={"f": 3}, absent={"f": GRACE_REFRESHES})
        overrides = age(overrides, pool={"f"})
        assert overrides == Overrides(pins={"f": 3})

    def test_new_overrides_start_with_no_absence(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        del scene.live["f"]
        scene.refresh()
        assert scene.overrides.absent == {"f": 1}
        overrides = place(scene.overrides, scene.state, "f", 2, CONFIGURED)
        assert overrides.absent == {}

    def test_channels_removed_from_the_config_clear_at_once(self):
        overrides = Overrides(pins={"f": 3, "a": 0}, benches=frozenset({"d"}), absent={"f": 1})
        assert drop_unconfigured(overrides, {"a"}) == Overrides(pins={"a": 0})


class TestBoundBench:
    """A bench that place makes ends with the pin that made it."""

    def test_place_binds_the_displaced_bench_to_the_new_pin(self, scene):
        overrides = place(scene.overrides, scene.state, "f", 3, CONFIGURED)
        assert overrides.bound == {"d": "f"}

    def test_waiting_works_with_a_bound_bench(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        assert scene.waiting() == []

    def test_unpin_releases_the_bound_bench(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        overrides = unpin(scene.overrides, 3)
        assert overrides == Overrides()
        # D competes again; F (50 + 30 stability) still outranks it (70)
        assert scene.act(overrides) == ("a", "b", "c", "f")

    def test_benching_the_pinned_channel_releases_its_bench(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        overrides = bench(scene.overrides, "f", CONFIGURED)
        assert overrides == Overrides(benches=frozenset({"f"}))

    def test_benching_the_bound_channel_again_unbinds_it(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        scene.act(bench(scene.overrides, "d", CONFIGURED))
        assert scene.overrides.bound == {}
        assert unpin(scene.overrides, 3).benches == {"d"}

    def test_unbench_unbinds(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        overrides = unbench(scene.overrides, "d", CONFIGURED)
        assert overrides == Overrides(pins={"f": 3})

    def test_a_new_place_on_the_slot_releases_the_old_pins_bench(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        overrides = place(scene.overrides, scene.state, "e", 3, CONFIGURED)
        assert overrides.pins == {"e": 3}
        assert overrides.benches == {"f"}
        assert overrides.bound == {"f": "e"}

    def test_placing_the_bound_channel_unbinds_it(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        overrides = place(scene.overrides, scene.state, "d", 0, CONFIGURED)
        assert overrides.pins == {"f": 3, "d": 0}
        assert overrides.bound == {"a": "d"}

    def test_swap_keeps_the_binding(self, scene):
        scene.act(place(scene.overrides, scene.state, "f", 3, CONFIGURED))
        overrides, _ = swap(scene.overrides, scene.state, 0, 3)
        assert overrides.pins == {"f": 0}
        assert overrides.bound == {"d": "f"}

    def test_an_expired_pin_releases_its_bench(self):
        overrides = Overrides(pins={"f": 3}, benches=frozenset({"d"}), bound={"d": "f"})
        for _ in range(GRACE_REFRESHES + 1):
            overrides = age(overrides, pool={"a", "d"})
        assert overrides == Overrides()

    def test_a_binding_needs_its_bench_and_its_pin(self):
        with pytest.raises(ValueError):
            Overrides(pins={"f": 3}, bound={"d": "f"})
        with pytest.raises(ValueError):
            Overrides(benches=frozenset({"d"}), bound={"d": "f"})
