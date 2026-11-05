""" "Not new" marks for "New to you" (WHO-123, WHO-124).

A mark is per user and has no period: a marked item is never new again, in
any period (rule 1). One mark is one ``not_new_mark`` row, made by the user
(``source = 'user'``) or read from their Spotify library when they connected
(``spotify_library``, ``app.spotify.library_baseline``). Both hide the same way:

- ``album``: the Spotify album ID, of an album, single or EP. The release and
  every track the user ever played on it are not new (rule 2). This is read at
  query time from the user's plays, so a track of that album first played
  after the mark is not new either.
- ``track``: the ``item_key``. Only that track (rule 4).
- ``artist``: the artist name after NFC and trimming (R-31). The app no longer
  lists artists, so the API takes no new artist marks (``MARKABLE``); rows the
  library baseline writes are kept and read by nothing.

Removing a mark (Undo) deletes its row, so an album's tracks come back with it
(rule 5).

``NOT_NEW`` is SQL for the read queries in ``app.read.new_to_you`` and
``app.read.album_tracks``. It needs their ``user_events`` CTE (with
``album_id``, ``item_key`` and ``creators``) and the ``:user_id`` parameter.
Every set it builds holds no NULL, so ``NOT IN`` on it is safe.
"""

import re
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from app.read.names import ARTIST_NAME

Kind = Literal["album", "artist", "track"]
KINDS: Final[tuple[Kind, ...]] = ("album", "artist", "track")
# What the mark routes take: there is no artist list to mark from.
MARKABLE: Final[tuple[Kind, ...]] = ("album", "track")

# Spotify IDs are base62 (spotify_album_id is only ever Spotify's album.id).
SPOTIFY_ID: Final = re.compile("[0-9A-Za-z]{1,64}")
# ``app.events.keys``: "id:" + the Spotify track ID, or "nt:" + 32 hex digits.
_ITEM_KEY: Final = re.compile("id:[0-9A-Za-z]{1,64}|nt:[0-9a-f]{32}")
# An artist name as served in ``artists.items[].name``. Bounded so a request
# cannot store an arbitrary blob; NUL is refused because Postgres text cannot hold it.
MAX_ARTIST_NAME_LENGTH: Final = 512

NOT_NEW = """
    marked AS (
        SELECT kind, item_id FROM not_new_mark WHERE user_id = :user_id
    ),
    not_new_albums AS (
        SELECT item_id AS album_id FROM marked WHERE kind = 'album'
    ),
    not_new_tracks AS (
        SELECT item_id AS item_key FROM marked WHERE kind = 'track'
        UNION
        SELECT item_key FROM user_events
        WHERE album_id IN (SELECT album_id FROM not_new_albums)
    )
"""


def is_valid_item_id(kind: Kind, item_id: str) -> bool:
    if kind == "album":
        return SPOTIFY_ID.fullmatch(item_id) is not None
    if kind == "track":
        return _ITEM_KEY.fullmatch(item_id) is not None
    return 0 < len(item_id) <= MAX_ARTIST_NAME_LENGTH and "\x00" not in item_id


# Only an item the user has a Spotify play of can be marked by the user, so
# their own marks never name anything their history does not already name. The
# library baseline (``source = 'spotify_library'``, ``app.spotify.library_baseline``)
# is the one writer of marks for items not played yet. An artist name matches
# only in its R-31 form, the form the list serves.
_PLAYED: Final[dict[Kind, str]] = {
    "album": "external_ids ->> 'spotify_album_id' = :item_id",
    "track": "item_key = :item_id",
    "artist": (
        "EXISTS (SELECT 1 FROM unnest(creators) AS creator(raw) " f"WHERE {ARTIST_NAME} = :item_id)"
    ),
}


def has_played(conn: Connection, *, user_id: UUID, kind: Kind, item_id: str) -> bool:
    played: bool = conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM unified_event "
            f"WHERE user_id = :user_id AND source = 'spotify' AND {_PLAYED[kind]})"
        ),
        {"user_id": user_id, "item_id": item_id},
    ).scalar_one()
    return played


def add_mark(conn: Connection, *, user_id: UUID, kind: Kind, item_id: str) -> None:
    """Idempotent: marking an item twice keeps the first mark and its ``created_at``."""
    conn.execute(
        text(
            "INSERT INTO not_new_mark (user_id, kind, item_id) VALUES (:user_id, :kind, :item_id) "
            "ON CONFLICT (user_id, kind, item_id) DO NOTHING"
        ),
        {"user_id": user_id, "kind": kind, "item_id": item_id},
    )


def remove_mark(conn: Connection, *, user_id: UUID, kind: Kind, item_id: str) -> None:
    """Idempotent: removing a mark that is not there does nothing."""
    conn.execute(
        text(
            "DELETE FROM not_new_mark "
            "WHERE user_id = :user_id AND kind = :kind AND item_id = :item_id"
        ),
        {"user_id": user_id, "kind": kind, "item_id": item_id},
    )
