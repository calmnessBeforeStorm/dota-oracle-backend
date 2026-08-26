"""Shared HTTP plumbing for every external source (spec sections 2, 4.4).

Rate limits are not advisory here: Liquipedia bans by IP, and the OpenDota monthly quota
is a hard budget for the backfill. Every client goes through this class.
"""

import asyncio
from types import TracebackType
from typing import Any, Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import get_logger

log = get_logger(__name__)


class RateLimitedError(Exception):
    """429 from an upstream source."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class BaseClient:
    """Async HTTP client with a minimum interval between requests and backoff on failure."""

    base_url: str = ""
    min_interval: float = 0.0  # seconds between requests
    user_agent: str = "dota-oracle/0.1"

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": self.user_agent},
        )
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._last_request_at + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = asyncio.get_running_loop().time()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def get_json(self, path: str, **params: Any) -> Any:
        await self._throttle()
        query = {k: v for k, v in params.items() if v is not None}
        response = await self._client.get(path, params=query)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitedError(float(retry_after) if retry_after else None)
        response.raise_for_status()
        return response.json()

    async def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        await self._throttle()
        response = await self._client.post(path, json=payload)
        response.raise_for_status()
        return response.json()
