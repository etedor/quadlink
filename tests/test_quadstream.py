"""Tests for QuadStream API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from quadlink.quadstream import QuadStreamClient
from quadlink.types import Quad


class TestQuadStreamClientInit:
    """Tests for client initialization."""

    def test_default_initialization(self):
        """Should initialize with provided credentials."""
        client = QuadStreamClient("user", "secret")

        assert client.username == "user"
        assert client.secret == "secret"
        assert client.timeout == 30
        assert client.cookies is None
        assert client.short_id is None

    def test_custom_timeout(self):
        """Should accept custom timeout."""
        client = QuadStreamClient("user", "secret", timeout=60)
        assert client.timeout == 60


class TestLogin:
    """Tests for login functionality."""

    @pytest.mark.asyncio
    async def test_successful_login(self):
        """Should store cookies and short_id on successful login."""
        client = QuadStreamClient("user", "secret")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"short_id": "abc123"}
        mock_response.cookies = httpx.Cookies()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.login()

        assert result is True
        assert client.short_id == "abc123"
        assert client.cookies is not None

    @pytest.mark.asyncio
    async def test_login_failure_status(self):
        """Should return False on non-200 status."""
        client = QuadStreamClient("user", "secret")

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.login()

        assert result is False
        assert client.short_id is None

    @pytest.mark.asyncio
    async def test_login_missing_short_id(self):
        """Should return False when response missing short_id."""
        client = QuadStreamClient("user", "secret")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # no short_id
        mock_response.cookies = httpx.Cookies()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.login()

        assert result is False

    @pytest.mark.asyncio
    async def test_login_timeout(self):
        """Should return False on timeout."""
        client = QuadStreamClient("user", "secret")

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.login()

        assert result is False

    @pytest.mark.asyncio
    async def test_login_request_error(self):
        """Should return False on request error."""
        client = QuadStreamClient("user", "secret")

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.RequestError("connection failed")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.login()

        assert result is False

    @pytest.mark.asyncio
    async def test_login_unexpected_error(self):
        """Should return False on unexpected error."""
        client = QuadStreamClient("user", "secret")

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = RuntimeError("unexpected")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.login()

        assert result is False


class TestUpdateQuad:
    """Tests for quad update functionality."""

    @pytest.mark.asyncio
    async def test_update_not_logged_in(self):
        """Should return False when not logged in."""
        client = QuadStreamClient("user", "secret")
        quad = Quad(stream1="https://stream1", stream2="https://stream2")

        result = await client.update_quad(quad)

        assert result is False

    @pytest.mark.asyncio
    async def test_successful_update(self):
        """Should return True on successful update."""
        client = QuadStreamClient("user", "secret")
        # httpx.Cookies needs at least one cookie to be truthy
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"

        quad = Quad(stream1="https://stream1", stream2="https://stream2")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.update_quad(quad)

        assert result is True

    @pytest.mark.asyncio
    async def test_update_failure_status(self):
        """Should return False on non-200 status."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"

        quad = Quad(stream1="https://stream1")

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Server error"

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.update_quad(quad)

        assert result is False

    @pytest.mark.asyncio
    async def test_update_timeout(self):
        """Should return False on timeout."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"

        quad = Quad()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.update_quad(quad)

        assert result is False

    @pytest.mark.asyncio
    async def test_update_request_error(self):
        """Should return False on request error."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"

        quad = Quad()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.RequestError("connection failed")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.update_quad(quad)

        assert result is False

    @pytest.mark.asyncio
    async def test_update_unexpected_error(self):
        """Should return False on unexpected error."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"

        quad = Quad()

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = RuntimeError("unexpected")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.update_quad(quad)

        assert result is False


class TestSendWebhook:
    """Tests for webhook functionality."""

    @pytest.mark.asyncio
    async def test_empty_webhook_url(self):
        """Should return True when webhook URL is empty."""
        client = QuadStreamClient("user", "secret")

        result = await client.send_webhook("")

        assert result is True

    @pytest.mark.asyncio
    async def test_successful_webhook(self):
        """Should return True on successful webhook."""
        client = QuadStreamClient("user", "secret")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.send_webhook("https://webhook.example.com")

        assert result is True

    @pytest.mark.asyncio
    async def test_webhook_with_quad(self):
        """Should include quad in payload when provided."""
        client = QuadStreamClient("user", "secret")
        quad = Quad(stream1="https://stream1")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.send_webhook("https://webhook.example.com", quad)

            # verify payload included quad
            call_args = mock_client.post.call_args
            payload = call_args[1]["json"]
            assert "quad" in payload
            assert payload["event"] == "quad_updated"

        assert result is True

    @pytest.mark.asyncio
    async def test_webhook_accepts_multiple_success_codes(self):
        """Should accept 200, 201, 202, 204 as success."""
        client = QuadStreamClient("user", "secret")

        for status_code in [200, 201, 202, 204]:
            mock_response = MagicMock()
            mock_response.status_code = status_code

            with patch("httpx.AsyncClient") as MockClient:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_response
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                MockClient.return_value = mock_client

                result = await client.send_webhook("https://webhook.example.com")

            assert result is True, f"Status {status_code} should be success"

    @pytest.mark.asyncio
    async def test_webhook_failure_status(self):
        """Should return False on failure status."""
        client = QuadStreamClient("user", "secret")

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.send_webhook("https://webhook.example.com")

        assert result is False

    @pytest.mark.asyncio
    async def test_webhook_timeout(self):
        """Should return False on timeout."""
        client = QuadStreamClient("user", "secret")

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.send_webhook("https://webhook.example.com")

        assert result is False

    @pytest.mark.asyncio
    async def test_webhook_request_error(self):
        """Should return False on request error."""
        client = QuadStreamClient("user", "secret")

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.RequestError("connection failed")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.send_webhook("https://webhook.example.com")

        assert result is False

    @pytest.mark.asyncio
    async def test_webhook_unexpected_error(self):
        """Should return False on unexpected error."""
        client = QuadStreamClient("user", "secret")

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post.side_effect = RuntimeError("unexpected")
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            MockClient.return_value = mock_client

            result = await client.send_webhook("https://webhook.example.com")

        assert result is False
