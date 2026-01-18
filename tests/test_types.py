"""Tests for core data types."""

from quadlink.types import Metadata, PrioritizedStream, Quad, QuadPosition, Stream


def test_metadata_creation():
    """Test creating metadata."""
    meta = Metadata(author="testuser", category="TestGame", title="Test Title")
    assert meta.author == "testuser"
    assert meta.category == "TestGame"
    assert meta.title == "Test Title"


def test_stream_creation():
    """Test creating a stream."""
    meta = Metadata(author="testuser", category="TestGame", title="Test Title")
    stream = Stream(url="https://twitch.tv/testuser", metadata=meta)
    assert stream.url == "https://twitch.tv/testuser"
    assert stream.metadata.author == "testuser"
    assert stream.master_url is None


def test_stream_with_master_url():
    """Test stream with master m3u8 URL."""
    meta = Metadata(author="testuser", category="TestGame", title="Test Title")
    stream = Stream(
        url="https://twitch.tv/testuser",
        metadata=meta,
        master_url="https://example.com/master.m3u8",
    )
    assert stream.master_url == "https://example.com/master.m3u8"


def test_prioritized_stream():
    """Test prioritized stream."""
    meta = Metadata(author="testuser", category="TestGame", title="Test Title")
    stream = Stream(url="https://twitch.tv/testuser", metadata=meta)
    ps = PrioritizedStream(stream=stream, priority=100, tiebreaker=0.5)

    assert ps.stream.metadata.author == "testuser"
    assert ps.priority == 100
    assert ps.tiebreaker == 0.5
    assert ps.position is None


def test_prioritized_stream_with_position():
    """Test prioritized stream with position from previous quad."""
    meta = Metadata(author="testuser", category="TestGame", title="Test Title")
    stream = Stream(url="https://twitch.tv/testuser", metadata=meta)
    ps = PrioritizedStream(stream=stream, priority=100, tiebreaker=0.5, position=2)

    assert ps.position == 2


def test_quad_empty():
    """Test empty quad creation."""
    quad = Quad()
    assert quad.stream1 == ""
    assert quad.stream2 == ""
    assert quad.stream3 == ""
    assert quad.stream4 == ""
    assert quad.is_empty() is True


def test_quad_with_streams():
    """Test quad with stream URLs."""
    quad = Quad(
        stream1="https://twitch.tv/user1",
        stream2="https://twitch.tv/user2",
        stream3="https://twitch.tv/user3",
        stream4="https://twitch.tv/user4",
    )
    assert quad.stream1 == "https://twitch.tv/user1"
    assert quad.is_empty() is False


def test_quad_partial():
    """Test quad with only some streams filled."""
    quad = Quad(stream1="https://twitch.tv/user1", stream2="https://twitch.tv/user2")
    assert quad.stream1 == "https://twitch.tv/user1"
    assert quad.stream2 == "https://twitch.tv/user2"
    assert quad.stream3 == ""
    assert quad.stream4 == ""
    assert quad.is_empty() is False


def test_quad_to_dict():
    """Test converting quad to dictionary."""
    quad = Quad(
        stream1="url1",
        stream2="url2",
        stream3="url3",
        stream4="url4",
    )
    d = quad.to_dict()
    assert d == {
        "stream1": "url1",
        "stream2": "url2",
        "stream3": "url3",
        "stream4": "url4",
    }


def test_quad_to_list():
    """Test converting quad to list."""
    quad = Quad(stream1="url1", stream2="url2", stream3="url3", stream4="url4")
    lst = quad.to_list()
    assert lst == ["url1", "url2", "url3", "url4"]


def test_quad_is_empty():
    """Test quad emptiness check."""
    # completely empty
    assert Quad().is_empty() is True
    assert Quad("", "", "", "").is_empty() is True

    # partially filled
    assert Quad(stream1="url1").is_empty() is False
    assert Quad(stream4="url4").is_empty() is False

    # fully filled
    assert Quad("url1", "url2", "url3", "url4").is_empty() is False


def test_quad_position_creation():
    """Test creating QuadPosition."""
    qp = QuadPosition(
        author="testuser",
        category="TestGame",
        title="Test Title",
        url="https://twitch.tv/testuser",
        position=1,
    )
    assert qp.author == "testuser"
    assert qp.category == "TestGame"
    assert qp.title == "Test Title"
    assert qp.url == "https://twitch.tv/testuser"
    assert qp.position == 1


def test_quad_position_from_stream():
    """Test creating QuadPosition from Stream."""
    meta = Metadata(author="testuser", category="TestGame", title="Test Title")
    stream = Stream(url="https://twitch.tv/testuser", metadata=meta)

    qp = QuadPosition.from_stream(stream, position=2)

    assert qp.author == "testuser"
    assert qp.category == "TestGame"
    assert qp.title == "Test Title"
    assert qp.url == "https://twitch.tv/testuser"
    assert qp.position == 2
