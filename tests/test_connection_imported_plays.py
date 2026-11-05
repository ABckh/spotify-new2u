"""``has_imported_plays`` on ``GET /v1/me/connection`` (WHO-93 F2).

The app draws the "imported history ends / live collection begins" marker only
when the import stored a play from before ``collection_started_at``. An import
that found nothing still sets ``history_imported_at``, so that field alone
cannot decide it. The flag is computed per session user in the SQL itself.
"""

from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.poller.spotify import poll_connection
from fake_spotify import FakeSpotify
from test_connect_collect_delete import CODES, T_CONNECT, at, sign_in


def status(api: TestClient, token: str) -> Any:
    response = api.get("/v1/me/connection", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return response.json()


def connect_and_poll(
    api: TestClient,
    engine: Engine,
    fake_spotify: FakeSpotify,
    code: str,
) -> str:
    at(T_CONNECT)
    token, _ = sign_in(api, code)
    with engine.connect() as conn:
        user = conn.execute(
            text("SELECT user_id FROM spotify_connection WHERE spotify_account_id = :a"),
            {"a": fake_spotify.account_id},
        ).scalar_one()
    poll_connection(engine, fake_spotify.client(), user, now=T_CONNECT + timedelta(seconds=10))
    return token


def test_an_empty_import_sets_history_imported_at_but_not_the_flag(
    api: TestClient, engine: Engine, fake_spotify: FakeSpotify
) -> None:
    token = connect_and_poll(api, engine, fake_spotify, CODES[0])

    body = status(api, token)
    assert body["history_imported_at"] is not None
    assert body["has_imported_plays"] is False


def test_an_import_of_only_later_plays_does_not_set_the_flag(
    api: TestClient, engine: Engine, fake_spotify: FakeSpotify
) -> None:
    # Played after connecting, before the first poll: imported, but not "earlier".
    fake_spotify.play("track-live", "Synthetic Live", T_CONNECT + timedelta(seconds=2))
    token = connect_and_poll(api, engine, fake_spotify, CODES[0])

    with engine.connect() as conn:
        assert conn.execute(text("SELECT ingest_method FROM unified_event")).scalars().all() == [
            "api_backfill"
        ]
    assert status(api, token)["has_imported_plays"] is False


def test_an_import_of_earlier_plays_sets_the_flag(
    api: TestClient, engine: Engine, fake_spotify: FakeSpotify
) -> None:
    for i in range(3):
        fake_spotify.play(
            f"track-old-{i}", f"Synthetic Old {i}", T_CONNECT - timedelta(hours=i + 1)
        )
    token = connect_and_poll(api, engine, fake_spotify, CODES[0])

    body = status(api, token)
    assert body["history_imported_at"] is not None
    assert body["has_imported_plays"] is True


def test_another_users_imported_plays_never_set_the_flag(
    api: TestClient, engine: Engine, fake_spotify: FakeSpotify
) -> None:
    # User A imports earlier plays.
    fake_spotify.play("track-old", "Synthetic Old", T_CONNECT - timedelta(hours=1))
    token_a = connect_and_poll(api, engine, fake_spotify, CODES[0])
    # User B, another Spotify account, whose import finds nothing earlier.
    fake_spotify.account_id = "synthetic-account-2"
    fake_spotify.history.clear()
    token_b = connect_and_poll(api, engine, fake_spotify, CODES[1])

    assert status(api, token_a)["has_imported_plays"] is True
    assert status(api, token_b)["has_imported_plays"] is False


def test_the_old_import_switch_must_still_be_a_json_bool(api: TestClient) -> None:
    # WHO-93 A1. The field is ignored since WHO-114, but older builds send it.
    for value in ("yes", "true", "1", "on", 1):
        response = api.post(
            "/auth/spotify/login",
            json={"home_timezone": "Europe/Helsinki", "import_recent_history": value},
        )
        assert response.status_code == 422, value
