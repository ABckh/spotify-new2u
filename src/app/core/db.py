"""Database engine and session factory.

One synchronous engine per process (the API process and the poller process each
build their own). Sessions are short-lived and explicitly scoped by the caller.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Every connection runs with the session time zone set to UTC (schema v1 §5.4),
# so nothing formatted or truncated in SQL depends on the server's zone. The
# test cluster runs in America/Los_Angeles to catch a regression here.
UTC_SESSION: dict[str, str] = {"options": "-c timezone=UTC"}


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""
    settings = get_settings()
    return create_engine(
        settings.database_url,
        # Recycle before Postgres' default idle timeouts bite the long-lived poller.
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
        # Bound values are play data; keep them out of exception text (PRD P12).
        hide_parameters=True,
        connect_args=UTC_SESSION,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a session, committing on success and rolling back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
