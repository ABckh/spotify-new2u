"""Test fixtures.

The test environment variables are set before any application module is
imported, so ``Settings`` never reads a developer's real .env during a test run.
These values are fake and are only ever used in tests.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
# A throwaway AES-256 key (32 bytes, URL-safe base64). Tests only.
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "pOTtJ4Ua5bTM3qJ0Rw1wZxGkT6k8Q0rT9WiKx9Yl8qA=")
os.environ.setdefault("SESSION_TOKEN_SECRET", "test-session-secret")
os.environ.setdefault("SPOTIFY_CLIENT_ID", "test-client-id")
os.environ.setdefault("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/auth/spotify/callback")
os.environ.setdefault("APP_CALLBACK_URL", "new2u-test://signed-in")

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402  # must come after the environment is primed
from alembic import command as alembic_command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Connection, Engine, create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.api.auth import get_db_engine, get_spotify_client, utc_now  # noqa: E402
from app.api.main import app  # noqa: E402
from app.api.rate_limit import export_streams, limiter  # noqa: E402
from app.core.db import UTC_SESSION  # noqa: E402
from fake_spotify import FakeSpotify  # noqa: E402
from read_helpers import NOW  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """An engine on DATABASE_URL with every migration applied.

    The privacy tests read the real schema, so they need a real Postgres. Locally
    they skip when none is reachable; in CI (``CI`` is set) that is a failure,
    because a skipped privacy test proves nothing.
    """
    engine = create_engine(os.environ["DATABASE_URL"], connect_args=UTC_SESSION)
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        engine.dispose()
        if os.environ.get("CI"):
            raise
        pytest.skip(f"no Postgres at DATABASE_URL ({type(exc).__name__})")
    alembic_command.upgrade(AlembicConfig("alembic.ini"), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is always rolled back.

    Postgres DDL is transactional, so tests may create throwaway schemas here.
    """
    with migrated_engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def engine(migrated_engine: Engine) -> Iterator[Engine]:
    """The migrated engine for code that commits its own transactions.

    Every user-scoped row is removed afterwards (``app_user`` cascades), and so
    are sign-ins in progress, so tests do not see each other's data.
    """
    try:
        yield migrated_engine
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(text("DELETE FROM app_user"))
            conn.execute(text("DELETE FROM oauth_login"))


@pytest.fixture
def fake_spotify() -> FakeSpotify:
    return FakeSpotify()


@pytest.fixture
def api(engine: Engine, fake_spotify: FakeSpotify) -> Iterator[TestClient]:
    """The API on the test database, talking to the fake Spotify."""
    app.dependency_overrides[get_db_engine] = lambda: engine
    app.dependency_overrides[get_spotify_client] = lambda: fake_spotify.client()
    limiter.clear()
    export_streams.clear()
    try:
        # The callback redirects to the app's URL scheme; tests read the Location.
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def read_api(api: TestClient) -> TestClient:
    """The API with its clock fixed at ``read_helpers.NOW``."""
    app.dependency_overrides[utc_now] = lambda: NOW
    return api
