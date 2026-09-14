"""Holds the current per-slot Twitch media-playlist URL and channel identity.

Written by the daemon each cycle, read by the slot relay. In-process only —
daemon loop and aiohttp app share one event loop, so no locking is needed.
"""

from typing import NamedTuple


class SlotEntry(NamedTuple):
    """A slot's current channel identity and its (re-minted) playlist URL."""

    identity: str  # stable channel identity (does not churn across cycles)
    url: str  # freshly-resolved Twitch media-playlist URL (churns each cycle)


class SlotStore:
    """Current channel identity + media-playlist URL for each of the 4 slots."""

    def __init__(self) -> None:
        self._entries: dict[int, SlotEntry | None] = {1: None, 2: None, 3: None, 4: None}

    def update(self, urls: list[str], identities: list[str]) -> None:
        """Replace all 4 slots from a quad's ordered URL and identity lists.

        An empty URL maps to None (an empty slot).
        """
        for i in range(4):
            url = urls[i] if i < len(urls) else ""
            identity = identities[i] if i < len(identities) else ""
            self._entries[i + 1] = SlotEntry(identity, url) if url else None

    def get(self, slot: int) -> SlotEntry | None:
        """Return the current entry for a slot, or None if empty/unknown."""
        return self._entries.get(slot)
