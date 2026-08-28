"""Shared HTTP plumbing for every external source (spec sections 2, 4.4).

Rate limits are not advisory here: Liquipedia bans by IP, and the OpenDota monthly quota
is a hard budget for the backfill. Every client goes through this class.
"""

import asyncio
import re
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


#: Credentials we send as query parameters. httpx puts the full URL into the text of
#: HTTPStatusError, which then lands in structlog and Sentry, so it gets scrubbed first.
#: Anchored on the query separator and longest-alternative-first, so `api_key=` matches
#: as a whole rather than as a bare `key=`.
_SECRET_RE = re.compile(
    r"([?&])(access_token|api_key|token|key)=[^&\s'\"]+",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Replace the value of any credential query parameter with ***.

    Applied to anything derived from a request URL before it reaches a log line or an
    exception message. Cheap, and the alternative is an API key sitting in Sentry forever.
    """
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}=***", text)


class RateLimitedError(Exception):
    """429 from an upstream source.

    Distinct from a transport failure on purpose: retrying it faster makes it worse, and a
    caller running a long backfill needs to stop rather than work through the rest of its
    list at one failure per second - which is how an IP ban is earned.
    """

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(
            "rate limited"
            + (f", retry after {retry_after:.0f}s" if retry_after else ", no Retry-After given")
        )
        self.retry_after = retry_after


def _check(response: httpx.Response) -> None:
    """Turn a 429 into `RateLimitedError` and any other failure into a redacted status error.

    Separate from `raise_for_status` because a 429 is not an error in the same sense: it is
    the server stating a rate, and the callers that stop a run rather than push through it
    key off this exception type.
    """
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(float(retry_after) if retry_after else None)
    raise_for_status(response)


#: Shared by every request method. 429 is retried like the rest, but waits far longer than a
#: transport hiccup: the server has said it wants less traffic, and honouring that is the
#: point.
_retrying = retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError, RateLimitedError)),
    wait=wait_exponential(multiplier=2, min=2, max=120),
    stop=stop_after_attempt(4),
    reraise=True,
)


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

    @_retrying
    async def get_json(self, path: str, **params: Any) -> Any:
        await self._throttle()
        query = {k: v for k, v in params.items() if v is not None}
        response = await self._client.get(path, params=query)
        _check(response)
        return response.json()

    @_retrying
    async def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        """Same policy as `get_json`, and not by accident.

        It used to have neither the retry nor the 429 check, which mattered because the one
        client that posts is STRATZ - the source the whole training set comes from. Its 429s
        arrived as plain status errors, so the detail backfill counted them as ordinary
        failures and worked through the rest of its list at one rejected request every two
        seconds. That is the IP ban this module exists to avoid, reached by the route of
        looking like it was still working.
        """
        await self._throttle()
        response = await self._client.post(path, json=payload)
        _check(response)
        return response.json()


def raise_for_status(response: httpx.Response) -> None:
    """`response.raise_for_status()` with credentials stripped from the message.

    The exception still carries the original request object, so log `str(exc)` and never
    `exc.request.url` - the latter is unredacted.
    """
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(
            redact(str(exc)), request=exc.request, response=exc.response
        ) from None
