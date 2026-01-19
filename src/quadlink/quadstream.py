"""QuadStream API client for authentication and quad updates."""

import time
from typing import Any

import httpx
import structlog

from quadlink.types import Quad

logger = structlog.get_logger()

# renew session at 50% of lifetime (like DHCP T1)
SESSION_RENEWAL_RATIO = 0.5


class QuadStreamClient:
    """Client for QuadStream API operations."""

    BASE_URL = "https://quadstream.tv"

    def __init__(self, username: str, secret: str, timeout: int = 30):
        """
        Initialize QuadStream client.

        Args:
            username: QuadStream username
            secret: QuadStream secret/password
            timeout: HTTP request timeout in seconds
        """
        self.username = username
        self.secret = secret
        self.timeout = timeout
        self.cookies: httpx.Cookies | None = None
        self.short_id: str | None = None
        self._session_started_at: float | None = None
        self._session_expires_at: float | None = None

    def _extract_session_expiry(self) -> None:
        """Extract earliest cookie expiration time from session cookies."""
        self._session_started_at = time.time()

        if not self.cookies:
            self._session_expires_at = None
            return

        # httpx.Cookies wraps http.cookiejar - iterate to get Cookie objects
        expires_times: list[float] = []
        for cookie in self.cookies.jar:
            if cookie.expires is not None:
                expires_times.append(float(cookie.expires))

        if expires_times:
            self._session_expires_at = min(expires_times)
            lifetime = self._session_expires_at - self._session_started_at
            logger.debug(
                "session started",
                lifetime_seconds=lifetime,
                renew_at_seconds=lifetime * SESSION_RENEWAL_RATIO,
            )
        else:
            self._session_expires_at = None
            logger.debug("session cookies have no expiration")

    def _session_needs_refresh(self) -> bool:
        """Check if session should be refreshed."""
        if self._session_expires_at is None or self._session_started_at is None:
            return False
        lifetime = self._session_expires_at - self._session_started_at
        renewal_time = self._session_started_at + (lifetime * SESSION_RENEWAL_RATIO)
        return time.time() >= renewal_time

    async def login(self) -> bool:
        """
        Log in to QuadStream.

        Returns:
            True if login successful, False otherwise
        """
        url = f"{self.BASE_URL}/stream/api/login"

        payload = {
            "username": self.username,
            "secret": self.secret,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)

                if response.status_code != 200:
                    logger.error(
                        "quadstream login failed",
                        status=response.status_code,
                        response=response.text,
                    )
                    return False

                self.cookies = response.cookies
                self._extract_session_expiry()

                data = response.json()
                self.short_id = data.get("short_id")

                if not self.short_id:
                    logger.error("quadstream login response missing short_id")
                    return False

                logger.info("quadstream logged in", short_id=self.short_id)
                return True

        except httpx.TimeoutException:
            logger.error("quadstream login timeout")
            return False
        except httpx.RequestError as e:
            logger.error("quadstream login request error", error=str(e))
            return False
        except Exception as e:
            logger.error("quadstream login unexpected error", error=str(e))
            return False

    async def update_quad(self, quad: Quad, _retry: bool = True) -> bool:
        """
        Update quad on QuadStream.

        Args:
            quad: Quad with stream URLs
            _retry: Internal flag to prevent infinite retry loops

        Returns:
            True if update successful, False otherwise
        """
        if not self.cookies or not self.short_id:
            logger.error("not logged in to quadstream")
            return False

        # proactively re-auth if session is about to expire
        if self._session_needs_refresh():
            logger.info("session expiring soon, re-authenticating")
            if not await self.login():
                logger.error("proactive re-authentication failed")
                return False

        url = f"{self.BASE_URL}/stream/api/stream/{self.short_id}/update"
        quad_dict = quad.to_dict()

        logger.debug(
            "quadstream request",
            stream1=quad_dict.get("stream1", "")[:80] + "..." if quad_dict.get("stream1") else "",
            stream2=quad_dict.get("stream2", "")[:80] + "..." if quad_dict.get("stream2") else "",
            stream3=quad_dict.get("stream3", "")[:80] + "..." if quad_dict.get("stream3") else "",
            stream4=quad_dict.get("stream4", "")[:80] + "..." if quad_dict.get("stream4") else "",
        )

        try:
            async with httpx.AsyncClient(cookies=self.cookies, timeout=self.timeout) as client:
                response = await client.post(url, json=quad_dict)

                # session expired unexpectedly - re-auth and retry once
                if response.status_code == 403 and _retry:
                    logger.warning("quadstream session expired, re-authenticating")
                    if await self.login():
                        return await self.update_quad(quad, _retry=False)
                    return False

                if response.status_code != 200:
                    logger.error(
                        "quadstream update failed",
                        status=response.status_code,
                        response=response.text,
                    )
                    return False

                logger.info("quadstream updated")
                return True

        except httpx.TimeoutException:
            logger.error("quadstream update timeout")
            return False
        except httpx.RequestError as e:
            logger.error("quadstream update request error", error=str(e))
            return False
        except Exception as e:
            logger.error("quadstream update unexpected error", error=str(e))
            return False

    async def send_webhook(self, webhook_url: str, quad: Quad | None = None) -> bool:
        """
        Send webhook notification with quad data.

        Args:
            webhook_url: Webhook URL to trigger
            quad: Optional quad to include in payload

        Returns:
            True if webhook sent successfully, False otherwise
        """
        if not webhook_url:
            return True

        try:
            payload: dict[str, Any] = {"event": "quad_updated"}
            if quad:
                payload["quad"] = quad.to_dict()

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(webhook_url, json=payload)

                if response.status_code not in (200, 201, 202, 204):
                    logger.warning(
                        "webhook failed",
                        status=response.status_code,
                        url=webhook_url,
                    )
                    return False

                logger.info("webhook sent")
                return True

        except httpx.TimeoutException:
            logger.warning("webhook timeout", url=webhook_url)
            return False
        except httpx.RequestError as e:
            logger.warning("webhook error", url=webhook_url, error=str(e))
            return False
        except Exception as e:
            logger.warning("webhook error", url=webhook_url, error=str(e))
            return False
