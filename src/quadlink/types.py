"""Core data types for QuadLink."""

from dataclasses import dataclass


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
    """

    url: str
    metadata: Metadata
    master_url: str | None = None


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
        stream1: URL for first stream slot.
        stream2: URL for second stream slot.
        stream3: URL for third stream slot.
        stream4: URL for fourth stream slot.
    """

    stream1: str = ""
    stream2: str = ""
    stream3: str = ""
    stream4: str = ""

    def to_dict(self) -> dict[str, str]:
        """Convert to dictionary for API calls.

        Returns:
            Dict with stream1-stream4 keys mapped to URLs.
        """
        return {
            "stream1": self.stream1,
            "stream2": self.stream2,
            "stream3": self.stream3,
            "stream4": self.stream4,
        }

    def to_list(self) -> list[str]:
        """Convert to list of stream URLs.

        Returns:
            List of 4 stream URLs in position order.
        """
        return [self.stream1, self.stream2, self.stream3, self.stream4]

    def is_empty(self) -> bool:
        """Check if quad has no streams.

        Returns:
            True if all stream slots are empty.
        """
        return all(s == "" for s in self.to_list())


@dataclass
class QuadPosition:
    """Tracks a stream's position in the quad with its metadata.

    Attributes:
        author: Channel/streamer name.
        category: Game or category being streamed.
        title: Stream title.
        url: Stream URL.
        position: Quad position (0-3).
    """

    author: str
    category: str
    title: str
    url: str
    position: int

    @classmethod
    def from_stream(cls, stream: Stream, position: int) -> "QuadPosition":
        """Create from a Stream and position.

        Args:
            stream: Stream object with metadata.
            position: Quad position (0-3).

        Returns:
            QuadPosition instance.
        """
        return cls(
            author=stream.metadata.author,
            category=stream.metadata.category,
            title=stream.metadata.title,
            url=stream.url,
            position=position,
        )
