"""Only allowlisted keys reach ``unified_event.provider_extras`` (Security item 6b)."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.events.keys import item_key_from_provider_id
from app.events.store import PreciseEvent, WriteCounts, write_precise_event
from app.privacy.audit import unlisted_provider_extras_keys
from app.privacy.provider_extras import (
    PROVIDER_EXTRAS_ALLOWLIST,
    SPOTIFY_ALBUM_ARTIST_MAX_CHARS,
    SPOTIFY_ALBUM_ARTISTS_MAX_ITEMS,
    SPOTIFY_ALBUM_TOTAL_TRACKS_MAX,
    SPOTIFY_ALBUM_TYPES,
    ProviderExtrasKeyNotAllowed,
    check_provider_extras,
)
from app.privacy.stored_fields import Category

SELFTEST_SCHEMA = "privacy_selftest"


def test_unlisted_key_is_rejected() -> None:
    with pytest.raises(ProviderExtrasKeyNotAllowed) as excinfo:
        check_provider_extras("spotify", {"context": "synthetic-playlist-uri"})

    assert excinfo.value.keys == frozenset({"context"})


def test_rejection_names_the_key_but_never_the_value() -> None:
    with pytest.raises(ProviderExtrasKeyNotAllowed) as excinfo:
        check_provider_extras("spotify", {"context": "synthetic-playlist-uri"})

    assert "context" in str(excinfo.value)
    assert "synthetic-playlist-uri" not in str(excinfo.value)


def test_unknown_source_permits_nothing() -> None:
    with pytest.raises(ProviderExtrasKeyNotAllowed):
        check_provider_extras("trakt", {"episode_id": "1"})


def test_empty_extras_are_accepted() -> None:
    check_provider_extras("spotify", {})


@pytest.mark.parametrize("source", [*sorted(PROVIDER_EXTRAS_ALLOWLIST), "unlisted_source"])
def test_database_rejects_an_unlisted_key(db: Connection, source: str) -> None:
    """The CHECK constraint covers writers that skip ``check_provider_extras`` (WHO-24 R3)."""
    event_id = _stored_event(db)

    with pytest.raises(IntegrityError, match="unified_event_provider_extras_allowlist_chk"):
        with db.begin_nested():
            _set_extras(db, event_id, source, {"synthetic_unlisted_key": "x"})


# One valid value per allowlisted key: the migration that allows a key also bounds it.
VALID_SAMPLES: dict[str, object] = {
    "spotify_album_artists": ["Synthetic Label"],
    "spotify_album_total_tracks": 12,
    "spotify_album_type": "single",
}


def test_database_accepts_every_allowlisted_key(db: Connection) -> None:
    """A key added to the constant without a migration fails here, and the reverse above."""
    event_id = _stored_event(db)

    for source, keys in PROVIDER_EXTRAS_ALLOWLIST.items():
        _set_extras(db, event_id, source, {})
        for key in keys:
            assert key in VALID_SAMPLES, f"add a valid sample value for {key}"
            _set_extras(db, event_id, source, {key: VALID_SAMPLES[key]})


def test_album_artists_bounds_match_the_constants() -> None:
    assert SPOTIFY_ALBUM_ARTISTS_MAX_ITEMS == 20
    assert SPOTIFY_ALBUM_ARTIST_MAX_CHARS == 500


@pytest.mark.parametrize(
    "value",
    [
        ["A" * 500] * 20,  # the largest value allowed
        ["A"],
    ],
)
def test_database_accepts_album_artists_within_bounds(db: Connection, value: object) -> None:
    _set_extras(db, _stored_event(db), "spotify", {"spotify_album_artists": value})


@pytest.mark.parametrize(
    "value",
    [
        "Synthetic Label",  # not an array
        [],
        ["A"] * 21,
        ["A" * 501],
        [""],
        [1],
        [None],
        [["nested"]],
        {"name": "Synthetic Label"},
    ],
)
def test_database_rejects_album_artists_out_of_bounds(db: Connection, value: object) -> None:
    event_id = _stored_event(db)

    with pytest.raises(IntegrityError, match="unified_event_provider_extras_allowlist_chk"):
        with db.begin_nested():
            _set_extras(db, event_id, "spotify", {"spotify_album_artists": value})


def test_album_artists_are_spotify_only(db: Connection) -> None:
    event_id = _stored_event(db)

    with pytest.raises(IntegrityError, match="unified_event_provider_extras_allowlist_chk"):
        with db.begin_nested():
            _set_extras(db, event_id, "youtube", {"spotify_album_artists": ["Synthetic"]})


def test_album_total_tracks_bound_matches_the_constant() -> None:
    assert SPOTIFY_ALBUM_TOTAL_TRACKS_MAX == 100_000


@pytest.mark.parametrize("value", [1, 2, 100_000, 12.0])
def test_database_accepts_album_total_tracks_within_bounds(db: Connection, value: object) -> None:
    _set_extras(db, _stored_event(db), "spotify", {"spotify_album_total_tracks": value})


@pytest.mark.parametrize("value", [0, -1, 100_001, 1.5, "12", None, True, [12], {"n": 12}])
def test_database_rejects_album_total_tracks_out_of_bounds(db: Connection, value: object) -> None:
    event_id = _stored_event(db)

    with pytest.raises(IntegrityError, match="unified_event_provider_extras_allowlist_chk"):
        with db.begin_nested():
            _set_extras(db, event_id, "spotify", {"spotify_album_total_tracks": value})


def test_album_total_tracks_are_spotify_only(db: Connection) -> None:
    event_id = _stored_event(db)

    with pytest.raises(IntegrityError, match="unified_event_provider_extras_allowlist_chk"):
        with db.begin_nested():
            _set_extras(db, event_id, "netflix", {"spotify_album_total_tracks": 12})


def test_album_types_match_the_constant() -> None:
    assert SPOTIFY_ALBUM_TYPES == {"album", "single", "compilation"}


@pytest.mark.parametrize("value", sorted(SPOTIFY_ALBUM_TYPES))
def test_database_accepts_each_album_type(db: Connection, value: str) -> None:
    _set_extras(db, _stored_event(db), "spotify", {"spotify_album_type": value})


@pytest.mark.parametrize("value", ["", "appears_on", "Single", "ep", None, 1, ["single"]])
def test_database_rejects_any_other_album_type(db: Connection, value: object) -> None:
    event_id = _stored_event(db)

    with pytest.raises(IntegrityError, match="unified_event_provider_extras_allowlist_chk"):
        with db.begin_nested():
            _set_extras(db, event_id, "spotify", {"spotify_album_type": value})


def test_album_type_is_spotify_only(db: Connection) -> None:
    event_id = _stored_event(db)

    with pytest.raises(IntegrityError, match="unified_event_provider_extras_allowlist_chk"):
        with db.begin_nested():
            _set_extras(db, event_id, "youtube", {"spotify_album_type": "album"})


def _stored_event(db: Connection) -> UUID:
    user_id = db.execute(
        text("INSERT INTO app_user (home_timezone) VALUES ('UTC') RETURNING id")
    ).scalar_one()
    write_precise_event(
        db,
        user_id=user_id,
        home_timezone="UTC",
        event=PreciseEvent(
            source="spotify",
            source_artifact="spotify_recently_played",
            ingest_method="api_poll",
            kind="listen",
            occurred_at=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
            title="Synthetic Track",
            item_key=item_key_from_provider_id("synthetic-track"),
        ),
        ingest_batch_id=None,
        counts=WriteCounts(),
    )
    event_id: UUID = db.execute(
        text("SELECT id FROM unified_event WHERE user_id = :u"), {"u": user_id}
    ).scalar_one()
    return event_id


def _set_extras(db: Connection, event_id: UUID, source: str, extras: dict[str, object]) -> None:
    """Raw SQL on purpose: this is the writer that forgot ``check_provider_extras``."""
    db.execute(
        text(
            "UPDATE unified_event SET source = :source, provider_extras = CAST(:extras AS jsonb) "
            "WHERE id = :id"
        ),
        {"source": source, "extras": json.dumps(extras), "id": event_id},
    )


def test_no_stored_row_has_an_unlisted_key(db: Connection) -> None:
    """Always passes on the empty CI database; kept for an operator to run on production."""
    assert unlisted_provider_extras_keys(db) == set()


def test_selftest_a_stored_unlisted_key_is_reported(db: Connection) -> None:
    db.execute(text(f"CREATE SCHEMA {SELFTEST_SCHEMA}"))
    db.execute(
        text(
            f"CREATE TABLE {SELFTEST_SCHEMA}.unified_event "
            "(source text NOT NULL, provider_extras jsonb NOT NULL DEFAULT '{}')"
        )
    )
    db.execute(
        text(
            f"INSERT INTO {SELFTEST_SCHEMA}.unified_event (source, provider_extras) VALUES "
            """('spotify', '{}'), ('spotify', '{"context": "x"}'), ('youtube', '{"lang": "x"}')"""
        )
    )

    assert unlisted_provider_extras_keys(db, SELFTEST_SCHEMA) == {
        ("spotify", "context"),
        ("youtube", "lang"),
    }
    allow_context = {"spotify": {"context": (Category.TRACK_DETAILS, "Synthetic label")}}
    assert unlisted_provider_extras_keys(db, SELFTEST_SCHEMA, allow_context) == {
        ("youtube", "lang")
    }
