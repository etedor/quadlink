"""QuadStream API client for authentication and quad updates."""

from typing import Any

import httpx
import structlog

from quadlink.types import Quad

logger = structlog.get_logger()


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
                data = response.json()
                self.short_id = data.get("short_id")

                if not self.short_id:
                    logger.error("quadstream login response missing short_id")
                    return False

                logger.info(
                    "quadstream login successful",
                    short_id=self.short_id,
                )
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

    async def update_quad(self, quad: Quad) -> bool:
        """
        Update quad on QuadStream.

        Args:
            quad: Quad with stream URLs

        Returns:
            True if update successful, False otherwise
        """
        if not self.cookies or not self.short_id:
            logger.error("not logged in to quadstream")
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

                if response.status_code != 200:
                    logger.error(
                        "quadstream update failed",
                        status=response.status_code,
                        response=response.text,
                    )
                    return False

                logger.info("quadstream update successful")
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

                logger.info("webhook successful")
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
