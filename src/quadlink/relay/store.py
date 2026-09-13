"""Holds the current per-slot Twitch media-playlist URLs.

Written by the daemon each cycle, read by the slot relay. In-process only —
daemon loop and aiohttp app share one event loop, so no locking is needed.
"""


class SlotStore:
    """Current media-playlist URL for each of the 4 quad slots."""

    def __init__(self) -> None:
        self._urls: dict[int, str | None] = {1: None, 2: None, 3: None, 4: None}

    def update(self, urls: list[str]) -> None:
        """Replace all 4 slot URLs from a quad's ordered URL list.

        Empty strings map to None (an empty slot).
        """
        for i in range(4):
            value = urls[i] if i < len(urls) else ""
            self._urls[i + 1] = value or None

    def get(self, slot: int) -> str | None:
        """Return the current URL for a slot, or None if empty/unknown."""
        return self._urls.get(slot)
