"""Process-wide logging setup, shared by the API, the poller and the CLIs.

Everything goes to stderr, where Docker hands it to the host's log driver
(journald on the VPS, removed within 14 days; see ``docker-compose.vps.yml`` and
docs/self-hosting.md "Logs").

PRD P12: no log holds a token, an authorization code, a query string of the
sign-in callback, or the title, artist or album of anything the user played.
Two things here enforce that for lines we do not write by hand:

- Uvicorn's access log is switched off, because it prints the full request line
  including the query string (the OAuth callback's ``code`` and ``state``).
  ``app.api.access_log`` writes the replacement line, without the query string.
  httpx's per-request INFO line is raised to WARNING for the same reason.
- Database error text is withheld from tracebacks. Postgres puts row values in
  the error DETAIL ("Failing row contains (<title>, ...)") and SQLAlchemy adds
  the bound parameters, so ``str(exc)`` of a failed insert can hold a track
  title. The traceback keeps the frames, the exception class, the SQLSTATE and
  the constraint name, which is enough to debug. Uvicorn's own loggers go
  through the same handler, because a route's crash is logged on
  ``uvicorn.error``.
"""

import logging
import sys
import traceback
from types import TracebackType

import psycopg
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

WITHHELD = "message withheld: database error text can contain stored values"

_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]

# The standard library's wording, so a chained traceback reads as usual.
_CAUSE_LINK = "\nThe above exception was the direct cause of the following exception:\n\n"
_CONTEXT_LINK = "\nDuring handling of the above exception, another exception occurred:\n\n"


def configure_logging() -> None:
    """Attach one stderr handler to the root logger at ``LOG_LEVEL``."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(PrivacyFormatter(LOG_FORMAT))
    logging.basicConfig(level=get_settings().log_level, handlers=[handler])
    # Uvicorn configures this logger before it imports the app, so this runs after
    # it and wins, whatever flags uvicorn was started with.
    logging.getLogger("uvicorn.access").disabled = True
    # Uvicorn gives "uvicorn" its own handler and formatter and stops it from
    # propagating. A route's crash is logged on "uvicorn.error" through that
    # handler, so a database error's DETAIL (row values) would print in full.
    # Remove uvicorn's handlers and let both loggers propagate to the root handler.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        for uvicorn_handler in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(uvicorn_handler)
        uvicorn_logger.propagate = True
    # httpx logs every request at INFO with the full URL, query string included.
    # Our Spotify calls go through httpx; a URL is not something P12 lets us log.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class PrivacyFormatter(logging.Formatter):
    """A formatter whose tracebacks never include database error text."""

    def formatException(self, ei: _ExcInfo | tuple[None, None, None]) -> str:
        exc = ei[1]
        if exc is None:
            return super().formatException(ei)
        return format_exception_without_db_text(exc).rstrip("\n")


def format_exception_without_db_text(exc: BaseException, _seen: set[int] | None = None) -> str:
    """Like ``traceback.format_exception``, with DB error messages withheld.

    The whole ``__cause__`` / ``__context__`` chain is formatted the same way,
    because SQLAlchemy wraps the psycopg error and both carry the same text.
    """
    seen = set() if _seen is None else _seen
    seen.add(id(exc))
    parts: list[str] = []
    cause, context = exc.__cause__, exc.__context__
    if cause is not None and id(cause) not in seen:
        parts += [format_exception_without_db_text(cause, seen), _CAUSE_LINK]
    elif context is not None and not exc.__suppress_context__ and id(context) not in seen:
        parts += [format_exception_without_db_text(context, seen), _CONTEXT_LINK]
    if exc.__traceback__ is not None:
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_tb(exc.__traceback__))
    parts.append(f"{_qualified_name(exc)}: {_safe_message(exc)}\n")
    return "".join(parts)


def _qualified_name(exc: BaseException) -> str:
    cls = type(exc)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def _safe_message(exc: BaseException) -> str:
    db_error = exc.orig if isinstance(exc, DBAPIError) else exc
    if isinstance(db_error, psycopg.Error):
        diag = db_error.diag
        return (
            f"{WITHHELD} (sqlstate={db_error.sqlstate}, "
            f"constraint={diag.constraint_name}, table={diag.table_name})"
        )
    if isinstance(exc, DBAPIError):
        return WITHHELD
    return str(exc)
