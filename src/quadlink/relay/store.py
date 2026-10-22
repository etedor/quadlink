"""Holds the current per-slot Twitch media-playlist URL and channel identity.

Slots are numbered 0-3.

Written by the daemon each cycle, read by the slot relay. In-process only —
daemon loop and aiohttp app share one event loop, so no locking is needed.
"""

from typing import NamedTuple


class SlotEntry(NamedTuple):
    """A slot's current channel identity and the playlist URL the relay plays."""

    identity: str  # stable channel identity (does not churn across cycles)
    url: str  # kept while the channel holds the slot; see SlotStore.update


class SlotStore:
    """Current channel identity + media-playlist URL for each of the 4 slots."""

    def __init__(self) -> None:
        self._entries: dict[int, SlotEntry | None] = dict.fromkeys(range(4))
        # newest URL per slot from the last update, held back until the kept one fails
        self._fresh: dict[int, str] = {}
        # the channel a slot showed before its current one; the relay plays it
        # until the new channel's preroll ends
        self._outgoing: dict[int, SlotEntry] = {}

    def update(self, urls: list[str], identities: list[str]) -> None:
        """Set all 4 slots from a quad's ordered URL and identity lists.

        A slot whose channel is unchanged keeps its URL: each lookup mints a new
        URL, and Twitch opens every new URL as a new session with a preroll ad.
        An empty URL maps to None (an empty slot).
        """
        for slot in range(4):
            url = urls[slot] if slot < len(urls) else ""
            identity = identities[slot] if slot < len(identities) else ""
            if not url:
                self._entries[slot] = None
                self._fresh.pop(slot, None)
                self._outgoing.pop(slot, None)
                continue
            self._fresh[slot] = url
            current = self._entries[slot]
            if current is None or current.identity != identity:
                if current is not None:
                    self._outgoing[slot] = current
                outgoing = self._outgoing.get(slot)
                if outgoing is not None and outgoing.identity == identity:
                    del self._outgoing[slot]
                self._entries[slot] = SlotEntry(identity, url)

    def refresh(self, slot: int) -> bool:
        """Move a slot to its newest URL. False when there is no newer one."""
        entry = self._entries.get(slot)
        fresh = self._fresh.get(slot)
        if entry is None or fresh is None or fresh == entry.url:
            return False
        self._entries[slot] = entry._replace(url=fresh)
        return True

    def outgoing(self, slot: int) -> SlotEntry | None:
        """The channel a slot showed before its current one, until released."""
        return self._outgoing.get(slot)

    def release_outgoing(self, slot: int) -> None:
        """Forget a slot's outgoing channel once the current one plays."""
        self._outgoing.pop(slot, None)

    def get(self, slot: int) -> SlotEntry | None:
        """Return the current entry for a slot, or None if empty/unknown."""
        return self._entries.get(slot)
