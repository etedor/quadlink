"""Tests for the slot URL store."""

from quadlink.relay.store import SlotStore


def test_empty_store_returns_none():
    store = SlotStore()
    assert store.get(1) is None
    assert store.get(4) is None


def test_update_maps_urls_to_slots():
    store = SlotStore()
    store.update(["a", "b", "c", "d"])
    assert store.get(1) == "a"
    assert store.get(2) == "b"
    assert store.get(3) == "c"
    assert store.get(4) == "d"


def test_empty_string_becomes_none():
    store = SlotStore()
    store.update(["a", "", "c", ""])
    assert store.get(1) == "a"
    assert store.get(2) is None
    assert store.get(4) is None


def test_out_of_range_slot_returns_none():
    store = SlotStore()
    store.update(["a", "b", "c", "d"])
    assert store.get(0) is None
    assert store.get(5) is None
