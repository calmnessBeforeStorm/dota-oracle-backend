"""structlog JSON logging (spec section 9.5)."""

import logging

import structlog

from app.core.redaction import redact


class RedactingFilter(logging.Filter):
    """Scrub credentials out of every log record, whoever emitted it.

    `redact` was applied where we build a message ourselves, which covers our own lines and
    the exception text we raise. It does not cover libraries that log request URLs on their
    own account, and one of them does: httpx logs `HTTP Request: GET <url> "200 OK"` at INFO,
    so `docker compose logs worker` carried the full Steam API key on every poll - twice a
    minute, forever, in a stream people paste into issues.

    Installed on the root handler rather than on the httpx logger by name, because the next
    library to do this will not be httpx and naming them one at a time is a list nobody
    maintains. It is a filter and not a formatter so it applies whatever the output format is.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Render first, then redact, then keep the rendered text. Scrubbing `msg` and each
        # of `args` in place looks equivalent and is not: httpx logs
        # `'HTTP Request: %s %s "%s %d %s"'` and passes `request.url`, which is an
        # `httpx.URL` and not a `str`, so a per-argument type check walks straight past the
        # one argument that carries the key. Whatever a secret is wrapped in, it is a string
        # by the time it reaches `getMessage`.
        message = record.getMessage()
        scrubbed = redact(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
