"""Tests for QuadStream API client."""

import time
from http.cookiejar import Cookie
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from quadlink.quadstream import SESSION_RENEWAL_RATIO, QuadStreamClient
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
        assert client._session_started_at is None
        assert client._session_expires_at is None

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


def _make_cookie(name: str, value: str, expires: int | None = None) -> Cookie:
    """Create a cookie for testing."""
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain="quadstream.tv",
        domain_specified=True,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=True,
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


class TestSessionExpiry:
    """Tests for session expiry detection and refresh."""

    def test_extract_session_expiry_with_expires(self):
        """Should extract expiration and record start time."""
        client = QuadStreamClient("user", "secret")

        cookies = httpx.Cookies()
        future_time = int(time.time()) + 3600
        cookies.jar.set_cookie(_make_cookie("session", "abc", expires=future_time))
        client.cookies = cookies

        client._extract_session_expiry()

        assert client._session_expires_at == float(future_time)
        assert client._session_started_at is not None

    def test_extract_session_expiry_uses_earliest(self):
        """Should use earliest expiration when multiple cookies."""
        client = QuadStreamClient("user", "secret")

        cookies = httpx.Cookies()
        earlier = int(time.time()) + 1800
        later = int(time.time()) + 3600
        cookies.jar.set_cookie(_make_cookie("session", "abc", expires=earlier))
        cookies.jar.set_cookie(_make_cookie("other", "xyz", expires=later))
        client.cookies = cookies

        client._extract_session_expiry()

        assert client._session_expires_at == float(earlier)

    def test_extract_session_expiry_no_expires(self):
        """Should set None when cookies have no expiration."""
        client = QuadStreamClient("user", "secret")

        cookies = httpx.Cookies()
        cookies.jar.set_cookie(_make_cookie("session", "abc", expires=None))
        client.cookies = cookies

        client._extract_session_expiry()

        assert client._session_expires_at is None
        assert client._session_started_at is not None

    def test_extract_session_expiry_no_cookies(self):
        """Should set None when no cookies."""
        client = QuadStreamClient("user", "secret")
        client.cookies = None

        client._extract_session_expiry()

        assert client._session_expires_at is None

    def test_session_needs_refresh_no_expiry(self):
        """Should return False when no expiry tracked."""
        client = QuadStreamClient("user", "secret")
        client._session_expires_at = None

        assert client._session_needs_refresh() is False

    def test_session_needs_refresh_before_half_lifetime(self):
        """Should return False before 50% of session lifetime."""
        client = QuadStreamClient("user", "secret")
        now = time.time()
        client._session_started_at = now
        client._session_expires_at = now + 3600  # 1 hour session

        # at 0% of lifetime, should not need refresh
        assert client._session_needs_refresh() is False

    def test_session_needs_refresh_after_half_lifetime(self):
        """Should return True after 50% of session lifetime."""
        client = QuadStreamClient("user", "secret")
        now = time.time()
        client._session_started_at = now - 1900  # started 31+ min ago
        client._session_expires_at = now + 1700  # expires in ~28 min (1 hour total)

        # past 50% of lifetime, should need refresh
        assert client._session_needs_refresh() is True

    def test_session_needs_refresh_expired(self):
        """Should return True when session already expired."""
        client = QuadStreamClient("user", "secret")
        now = time.time()
        client._session_started_at = now - 3700
        client._session_expires_at = now - 100

        assert client._session_needs_refresh() is True

    def test_session_renewal_ratio(self):
        """Should use 50% renewal ratio like DHCP T1."""
        assert SESSION_RENEWAL_RATIO == 0.5


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

    @pytest.mark.asyncio
    async def test_update_403_retries_with_reauth(self):
        """Should re-authenticate and retry on 403."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"

        quad = Quad(stream1="https://stream1")

        mock_403_response = MagicMock()
        mock_403_response.status_code = 403
        mock_403_response.text = "Forbidden"

        mock_200_response = MagicMock()
        mock_200_response.status_code = 200

        with patch.object(client, "login", new_callable=AsyncMock) as mock_login:
            mock_login.return_value = True

            with patch("httpx.AsyncClient") as MockClient:
                mock_client = AsyncMock()
                mock_client.post.side_effect = [mock_403_response, mock_200_response]
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                MockClient.return_value = mock_client

                result = await client.update_quad(quad)

        assert result is True
        mock_login.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_403_fails_if_reauth_fails(self):
        """Should return False if re-auth after 403 fails."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"

        quad = Quad(stream1="https://stream1")

        mock_403_response = MagicMock()
        mock_403_response.status_code = 403
        mock_403_response.text = "Forbidden"

        with patch.object(client, "login", new_callable=AsyncMock) as mock_login:
            mock_login.return_value = False

            with patch("httpx.AsyncClient") as MockClient:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_403_response
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                MockClient.return_value = mock_client

                result = await client.update_quad(quad)

        assert result is False
        mock_login.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_403_no_retry_when_disabled(self):
        """Should not retry on 403 when _retry=False."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"

        quad = Quad(stream1="https://stream1")

        mock_403_response = MagicMock()
        mock_403_response.status_code = 403
        mock_403_response.text = "Forbidden"

        with patch.object(client, "login", new_callable=AsyncMock) as mock_login:
            with patch("httpx.AsyncClient") as MockClient:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_403_response
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                MockClient.return_value = mock_client

                result = await client.update_quad(quad, _retry=False)

        assert result is False
        mock_login.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_proactive_reauth_when_session_expiring(self):
        """Should proactively re-auth when past 50% of session lifetime."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"
        now = time.time()
        client._session_started_at = now - 1900  # started 31+ min ago
        client._session_expires_at = now + 1700  # 1 hour total, past 50%

        quad = Quad(stream1="https://stream1")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "login", new_callable=AsyncMock) as mock_login:
            mock_login.return_value = True

            with patch("httpx.AsyncClient") as MockClient:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_response
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                MockClient.return_value = mock_client

                result = await client.update_quad(quad)

        assert result is True
        mock_login.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_no_proactive_reauth_when_session_fresh(self):
        """Should not re-auth when before 50% of session lifetime."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"
        now = time.time()
        client._session_started_at = now  # just started
        client._session_expires_at = now + 3600  # 1 hour session

        quad = Quad(stream1="https://stream1")

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch.object(client, "login", new_callable=AsyncMock) as mock_login:
            with patch("httpx.AsyncClient") as MockClient:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_response
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None
                MockClient.return_value = mock_client

                result = await client.update_quad(quad)

        assert result is True
        mock_login.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_fails_if_proactive_reauth_fails(self):
        """Should return False if proactive re-auth fails."""
        client = QuadStreamClient("user", "secret")
        cookies = httpx.Cookies()
        cookies.set("session", "test")
        client.cookies = cookies
        client.short_id = "abc123"
        now = time.time()
        client._session_started_at = now - 1900
        client._session_expires_at = now + 1700

        quad = Quad(stream1="https://stream1")

        with patch.object(client, "login", new_callable=AsyncMock) as mock_login:
            mock_login.return_value = False

            result = await client.update_quad(quad)

        assert result is False
        mock_login.assert_called_once()


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
