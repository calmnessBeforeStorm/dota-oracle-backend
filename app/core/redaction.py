"""Keeping credentials out of anything we write down (spec section 9.5).

Lives in `core` rather than next to the HTTP clients because it has two callers in different
layers and one of them is logging itself: `app.core.logging` installs it on the root handler,
and a client module cannot be imported from there without a cycle. The clients are the reason
it exists, but they are no longer the only reason it runs.
"""

import re

#: Credentials we send as query parameters. httpx puts the full URL into the text of
#: HTTPStatusError, which then lands in structlog and Sentry, and it also logs the URL itself
#: at INFO on every request - so it gets scrubbed on both paths.
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
