"""``DELETE /v1/me`` leaves no row for the caller in any table, tokens included.

Tables are read from the live catalogue and seeded with ``SEEDERS`` from
``test_privacy_delete``, so a table added later is covered here too.
"""

import logging
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.privacy.audit import rows_held_for_user
from read_helpers import User, add_session, make_user
from test_privacy_delete import SEEDERS


def seeded_user(engine: Engine) -> User:
    """A user with at least one row in every user table, and a live session."""
    user_id, token = uuid4(), f"synthetic-session-{uuid4()}"
    with engine.begin() as conn:
        for seed in SEEDERS.values():
            seed(conn, user_id)
        add_session(conn, user_id, token)
    return User(user_id, token, "Europe/Helsinki")


def test_delete_me_leaves_no_rows_in_any_table(
    read_api: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    gone, kept = seeded_user(engine), seeded_user(engine)
    with engine.connect() as conn:
        before = rows_held_for_user(conn, gone.id)
        kept_before = rows_held_for_user(conn, kept.id)
    assert all(n > 0 for n in before.values()), before

    response = read_api.delete("/v1/me", headers=gone.auth)

    assert response.status_code == 204
    assert response.content == b""
    with engine.connect() as conn:
        assert rows_held_for_user(conn, gone.id) == dict.fromkeys(before, 0)
        assert rows_held_for_user(conn, kept.id) == kept_before
    deleted_lines = [r.getMessage() for r in caplog.records if r.name == "app.privacy.delete"]
    assert deleted_lines == [f"account_deleted user_id={gone.id}"]


def test_a_second_delete_with_the_same_session_is_401(read_api: TestClient, engine: Engine) -> None:
    user = make_user(engine)

    assert read_api.delete("/v1/me", headers=user.auth).status_code == 204
    assert read_api.delete("/v1/me", headers=user.auth).status_code == 401
