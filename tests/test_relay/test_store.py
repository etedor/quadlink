"""Tests for the slot store."""

from quadlink.relay.store import SlotEntry, SlotStore


def test_empty_store_returns_none():
    store = SlotStore()
    assert store.get(1) is None
    assert store.get(4) is None


def test_update_maps_urls_and_identities_to_slots():
    store = SlotStore()
    store.update(["a", "b", "c", "d"], ["A", "B", "C", "D"])
    assert store.get(1) == SlotEntry("A", "a")
    assert store.get(2) == SlotEntry("B", "b")
    assert store.get(3) == SlotEntry("C", "c")
    assert store.get(4) == SlotEntry("D", "d")


def test_entry_exposes_identity_and_url():
    store = SlotStore()
    store.update(["a", "", "", ""], ["chan-a", "", "", ""])
    entry = store.get(1)
    assert entry is not None
    assert entry.identity == "chan-a"
    assert entry.url == "a"


def test_empty_url_becomes_none():
    store = SlotStore()
    store.update(["a", "", "c", ""], ["A", "B", "C", "D"])
    assert store.get(1) == SlotEntry("A", "a")
    assert store.get(2) is None  # empty url -> empty slot, even with an identity
    assert store.get(4) is None


def test_out_of_range_slot_returns_none():
    store = SlotStore()
    store.update(["a", "b", "c", "d"], ["A", "B", "C", "D"])
    assert store.get(0) is None
    assert store.get(5) is None


def test_unchanged_channel_keeps_its_url():
    store = SlotStore()
    store.update(["a1", "", "", ""], ["A", "", "", ""])
    store.update(["a2", "", "", ""], ["A", "", "", ""])
    assert store.get(1) == SlotEntry("A", "a1")


def test_new_channel_takes_new_url():
    store = SlotStore()
    store.update(["a1", "", "", ""], ["A", "", "", ""])
    store.update(["b1", "", "", ""], ["B", "", "", ""])
    assert store.get(1) == SlotEntry("B", "b1")


def test_refresh_moves_to_newest_url():
    store = SlotStore()
    store.update(["a1", "", "", ""], ["A", "", "", ""])
    store.update(["a2", "", "", ""], ["A", "", "", ""])
    assert store.refresh(1)
    assert store.get(1) == SlotEntry("A", "a2")
    assert not store.refresh(1)


def test_refresh_without_newer_url_is_false():
    store = SlotStore()
    store.update(["a1", "", "", ""], ["A", "", "", ""])
    assert not store.refresh(1)
    assert not store.refresh(2)


def test_emptied_slot_forgets_its_url():
    store = SlotStore()
    store.update(["a1", "", "", ""], ["A", "", "", ""])
    store.update(["", "", "", ""], ["", "", "", ""])
    store.update(["a2", "", "", ""], ["A", "", "", ""])
    assert store.get(1) == SlotEntry("A", "a2")
