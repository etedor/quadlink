"""Core data types for QuadLink."""

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit


def channel_url(url: str) -> str:
    """Expand a bare channel name or host-relative URL to a full channel URL."""
    if "://" in url:
        return url
    # "twitch.tv/foo" has a host already; only a bare name needs one
    if "/" in url:
        return f"https://{url}"
    return f"https://twitch.tv/{url}"


# first path segments that name a Twitch page, not a channel
_NOT_CHANNELS = frozenset(
    {
        "clip",
        "directory",
        "downloads",
        "friends",
        "inventory",
        "jobs",
        "messages",
        "moderator",
        "p",
        "popout",
        "search",
        "settings",
        "subscriptions",
        "turbo",
        "v",
        "video",
        "videos",
        "wallet",
    }
)


def channel_login(url: str) -> str:
    """The channel key for a configured URL or bare channel name.

    For a live channel this is its lowercase Twitch login; scheme, host ("www."
    or not), trailing slash and case do not change it, so every spelling of one
    channel maps to one key. Any other URL (a VOD, a clip, a directory page)
    keys on the URL itself, lowercased without scheme or "www.", so distinct
    pages never share a key.
    """
    parts = urlsplit(channel_url(url.strip()))
    host = parts.hostname or ""
    # player.twitch.tv/?channel=<login> names the channel in its query
    login = parse_qs(parts.query).get("channel", [""])[0]
    segments = parts.path.strip("/").split("/")
    page = len(segments) != 1 or segments[0].lower() in _NOT_CHANNELS
    # clips.twitch.tv/<slug> is a clip, not a channel
    if not login and not page and not host.startswith("clips."):
        login = segments[0]
    if login:
        return login.lower()
    host = host.removeprefix("www.")
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{parts.path.rstrip('/')}{query}".lower()


@dataclass
class Metadata:
    """Stream metadata from Twitch.

    Attributes:
        author: Channel/streamer name.
        category: Game or category being streamed.
        title: Stream title.
    """

    author: str
    category: str
    title: str


@dataclass
class Stream:
    """A Twitch stream with its metadata.

    Attributes:
        url: Original Twitch URL.
        metadata: Stream metadata (author, category, title).
        master_url: Master m3u8 playlist URL, if available.
        has_video: False when the only rendition carries audio alone.
        channel: Lowercase Twitch login, the channel's identity everywhere;
            derived from url when not given.
    """

    url: str
    metadata: Metadata
    master_url: str | None = None
    has_video: bool = True
    channel: str = ""

    def __post_init__(self) -> None:
        if not self.channel:
            self.channel = channel_login(self.url)


@dataclass
class PrioritizedStream:
    """A stream with priority and tiebreaker for selection.

    Attributes:
        stream: The stream object.
        priority: Priority level from config.
        tiebreaker: Random value for sorting equal-priority streams.
        position: Position in previous quad (0-3), if any.
    """

    stream: Stream
    priority: int
    tiebreaker: float
    position: int | None = None


@dataclass
class Quad:
    """A quad-view of 4 streams.

    Attributes:
        slots: Stream URL per slot 0-3 ("" for an empty slot); a shorter list
            is padded with empty slots.
    """

    slots: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.slots) > 4:
            raise ValueError("a quad holds at most 4 slots")
        self.slots = [*self.slots, *[""] * (4 - len(self.slots))]

    def to_dict(self) -> dict[str, str]:
        """Convert to the QuadStream API payload; the API names slots stream1-stream4."""
        return {f"stream{i + 1}": url for i, url in enumerate(self.slots)}

    def to_list(self) -> list[str]:
        """Convert to list of stream URLs.

        Returns:
            List of 4 stream URLs in slot order.
        """
        return list(self.slots)

    def is_empty(self) -> bool:
        """Check if quad has no streams.

        Returns:
            True if all stream slots are empty.
        """
        return all(s == "" for s in self.to_list())
