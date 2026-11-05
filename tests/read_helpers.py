"""Synthetic users, sessions and plays for the read API tests.

Plays go through ``write_precise_event``, the same writer the poller uses, so
the rows have real dedupe and day keys. Every value is synthetic.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from app.core.crypto import keyed_hash
from app.events.keys import ItemKey
from app.events.store import PreciseEvent, WriteCounts, write_precise_event

# The server clock in every read test, unless a test overrides it.
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class User:
    id: UUID
    token: str
    time_zone: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def make_user(engine: Engine, time_zone: str = "Europe/Warsaw") -> User:
    """An account with one live session, as if it had signed in."""
    token = secrets.token_urlsafe(32)
    with engine.begin() as conn:
        user_id: UUID = conn.execute(
            text("INSERT INTO app_user (home_timezone) VALUES (:tz) RETURNING id"),
            {"tz": time_zone},
        ).scalar_one()
        add_session(conn, user_id, token)
    return User(user_id, token, time_zone)


def add_session(conn: Connection, user_id: UUID, token: str) -> None:
    conn.execute(
        text(
            "INSERT INTO auth_session (user_id, login_id, token_hash, expires_at) "
            "VALUES (:u, :login, :hash, :expires)"
        ),
        {
            "u": user_id,
            "login": uuid4(),
            "hash": keyed_hash(token, purpose="session"),
            # Live for real time too: the session check uses the overridden clock,
            # but nothing else should ever see these rows as expired.
            "expires": datetime.now(UTC) + timedelta(days=3650),
        },
    )


def album_extras(total_tracks: int | None) -> dict[str, object]:
    """``provider_extras`` for an album of ``total_tracks`` tracks; None is an unknown count.

    The album read tests default to 1 track, so one played track makes an album
    listened, as the rule was before WHO-130.
    """
    return {} if total_tracks is None else {"spotify_album_total_tracks": total_tracks}


def add_play(
    engine: Engine,
    user: User,
    occurred_at: str | datetime,
    *,
    item: str = "AAA",
    title: str | None = None,
    creators: tuple[str, ...] = ("Nova Kett",),
    duration_ms: int | None = 200_000,
    played_ms: int | None = None,
    collection: str | None = None,
    artwork_url: str | None = None,
    external_ids: dict[str, str] | None = None,
    provider_extras: dict[str, object] | None = None,
) -> None:
    """Store one Spotify-like play. ``item`` becomes the item key ``id:<item>``."""
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at)
    with engine.begin() as conn:
        write_precise_event(
            conn,
            user_id=user.id,
            home_timezone=user.time_zone,
            event=PreciseEvent(
                source="spotify",
                source_artifact="spotify_recently_played",
                ingest_method="api_poll",
                kind="listen",
                occurred_at=occurred_at,
                title=title or f"Track {item}",
                item_key=ItemKey(f"id:{item}", "provider_id"),
                duration_ms=duration_ms,
                played_ms=played_ms,
                creators=creators,
                collection=collection,
                artwork_url=artwork_url,
                external_ids=external_ids or {},
                provider_extras=provider_extras or {},
            ),
            ingest_batch_id=None,
            counts=WriteCounts(),
        )


def get(api: TestClient, user: User, path: str, **params: str) -> dict[str, Any]:
    """GET a read route that must answer 200; returns the JSON body."""
    response = api.get(path, params=params, headers=user.auth)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body
