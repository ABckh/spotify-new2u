"""Album ratings and comments: the user's own score for an album, 1 to 10, and
their own words about it.

- One rating per user and album, with no period. Setting it again replaces
  it and moves ``rated_at``; clearing it deletes the row.
- One comment per user and album, the same way (``commented_at``). Free text,
  1 to ``MAX_COMMENT_LENGTH`` characters, not blank, no NUL (``is_valid_comment``).
- A rating and a comment are independent: two tables, so each can exist
  without the other, and setting or clearing one never touches the other.
  Neither is required.
- Only an album the user has a Spotify play of can be rated or commented on,
  the same check as "Not new" (``app.read.not_new.has_played``), so the tables
  never name an album the user's history does not.
- Every album item the read API serves carries ``rating`` and ``comment``
  (``ratings_for``, ``comments_for``), so the app shows them wherever the
  album is. The share picture shows the rating only, never the comment.
- Singles and EPs are rated and commented on the same way: to Spotify they
  are albums too.
- A track can have a comment too, by the same rules (``set_track_comment``),
  keyed by its ``item_key``: only a track the user has a play of. Every track
  item of the album screen and of search carries it (``track_comments_for``).
  Tracks have no rating.
- ``read_all_ratings`` is the all-time list: every rated release, best first,
  with its comment, if any; with ``commented``, also every release that has
  only a comment, after the rated ones, most recent first. Each has the name,
  artists and cover by the shared album rules
  (``app.read.album_sql.album_latest``) and its ``release_type``: ``album``, or
  ``single`` / ``ep`` (``release_kind``) for what Spotify calls a single. A
  release whose type is not stored yet is an album, as in the album lists.

A comment is the user's own text: it is never put in a log line or an error
message (PRD P12). The ``ValueError`` below says what is wrong, never the text.

Every statement is scoped by ``user_id = :user_id`` from the session.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from app.read.album_sql import ALBUM_EVENTS, album_latest, release_kind

ReleaseType = Literal["album", "single", "ep"]

MIN_RATING: Final = 1
MAX_RATING: Final = 10
# In characters (code points), as Postgres ``char_length`` counts them: a few
# paragraphs. Bounded so a request cannot store an arbitrary blob.
MAX_COMMENT_LENGTH: Final = 2000

# ALBUM_EVENTS needs a period; the all-time list uses none of its period columns.
_ALL_TIME = {"start": datetime(1970, 1, 1, tzinfo=UTC), "end": datetime(1970, 1, 1, tzinfo=UTC)}


@dataclass(frozen=True)
class RatedAlbum:
    album_id: str
    name: str | None
    artists: list[str]
    artwork_url: str | None
    release_type: ReleaseType
    # None only for a release listed for its comment alone.
    rating: int | None
    rated_at: datetime | None
    comment: str | None
    commented_at: datetime | None


def set_rating(conn: Connection, *, user_id: UUID, album_id: str, rating: int) -> None:
    if not MIN_RATING <= rating <= MAX_RATING:
        raise ValueError("rating is 1 to 10")
    conn.execute(
        text(
            "INSERT INTO album_rating (user_id, album_id, rating) "
            "VALUES (:user_id, :album_id, :rating) "
            "ON CONFLICT (user_id, album_id) DO UPDATE "
            "SET rating = EXCLUDED.rating, rated_at = now()"
        ),
        {"user_id": user_id, "album_id": album_id, "rating": rating},
    )


def clear_rating(conn: Connection, *, user_id: UUID, album_id: str) -> None:
    """Idempotent: clearing a rating that is not there does nothing."""
    conn.execute(
        text("DELETE FROM album_rating WHERE user_id = :user_id AND album_id = :album_id"),
        {"user_id": user_id, "album_id": album_id},
    )


def ratings_for(conn: Connection, *, user_id: UUID, album_ids: Iterable[str]) -> dict[str, int]:
    """The user's rating of each album that has one."""
    ids = sorted(set(album_ids))
    if not ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT album_id, rating FROM album_rating "
            "WHERE user_id = :user_id AND album_id = ANY(CAST(:ids AS text[]))"
        ),
        {"user_id": user_id, "ids": ids},
    )
    return {r.album_id: r.rating for r in rows}


def is_valid_comment(comment: str) -> bool:
    """1 to ``MAX_COMMENT_LENGTH`` characters, not only whitespace, and storable.

    NUL is refused because Postgres text cannot hold it, and a lone surrogate
    (``"\\ud800"`` in JSON) because it has no UTF-8 form. Line breaks are fine.
    """
    if not 0 < len(comment) <= MAX_COMMENT_LENGTH or not comment.strip() or "\x00" in comment:
        return False
    try:
        comment.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def set_comment(conn: Connection, *, user_id: UUID, album_id: str, comment: str) -> None:
    """Stores ``comment`` exactly as given, replacing the one before."""
    if not is_valid_comment(comment):
        raise ValueError(f"comment is 1 to {MAX_COMMENT_LENGTH} characters, not blank, no NUL")
    conn.execute(
        text(
            "INSERT INTO album_comment (user_id, album_id, comment) "
            "VALUES (:user_id, :album_id, :comment) "
            "ON CONFLICT (user_id, album_id) DO UPDATE "
            "SET comment = EXCLUDED.comment, commented_at = now()"
        ),
        {"user_id": user_id, "album_id": album_id, "comment": comment},
    )


def clear_comment(conn: Connection, *, user_id: UUID, album_id: str) -> None:
    """Idempotent: clearing a comment that is not there does nothing."""
    conn.execute(
        text("DELETE FROM album_comment WHERE user_id = :user_id AND album_id = :album_id"),
        {"user_id": user_id, "album_id": album_id},
    )


def comments_for(conn: Connection, *, user_id: UUID, album_ids: Iterable[str]) -> dict[str, str]:
    """The user's comment on each album that has one."""
    ids = sorted(set(album_ids))
    if not ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT album_id, comment FROM album_comment "
            "WHERE user_id = :user_id AND album_id = ANY(CAST(:ids AS text[]))"
        ),
        {"user_id": user_id, "ids": ids},
    )
    return {r.album_id: r.comment for r in rows}


def set_track_comment(conn: Connection, *, user_id: UUID, item_key: str, comment: str) -> None:
    """Stores ``comment`` on the track exactly as given, replacing the one before."""
    if not is_valid_comment(comment):
        raise ValueError(f"comment is 1 to {MAX_COMMENT_LENGTH} characters, not blank, no NUL")
    conn.execute(
        text(
            "INSERT INTO track_comment (user_id, item_key, comment) "
            "VALUES (:user_id, :item_key, :comment) "
            "ON CONFLICT (user_id, item_key) DO UPDATE "
            "SET comment = EXCLUDED.comment, commented_at = now()"
        ),
        {"user_id": user_id, "item_key": item_key, "comment": comment},
    )


def clear_track_comment(conn: Connection, *, user_id: UUID, item_key: str) -> None:
    """Idempotent: clearing a comment that is not there does nothing."""
    conn.execute(
        text("DELETE FROM track_comment WHERE user_id = :user_id AND item_key = :item_key"),
        {"user_id": user_id, "item_key": item_key},
    )


def track_comments_for(
    conn: Connection, *, user_id: UUID, item_keys: Iterable[str]
) -> dict[str, str]:
    """The user's comment on each track that has one."""
    keys = sorted(set(item_keys))
    if not keys:
        return {}
    rows = conn.execute(
        text(
            "SELECT item_key, comment FROM track_comment "
            "WHERE user_id = :user_id AND item_key = ANY(CAST(:keys AS text[]))"
        ),
        {"user_id": user_id, "keys": keys},
    )
    return {r.item_key: r.comment for r in rows}


# Best first, then the most recently rated, then the ID, so the order is total.
# Releases with only a comment (``:commented``) come after every rated one,
# the most recently commented first.
_ALL_RATINGS = text(
    f"WITH {ALBUM_EVENTS}, "
    "rated AS ("
    "    SELECT album_id, r.rating, r.rated_at, c.comment, c.commented_at "
    "    FROM (SELECT * FROM album_rating WHERE user_id = :user_id) r "
    "    FULL JOIN (SELECT * FROM album_comment WHERE user_id = :user_id) c USING (album_id) "
    "    WHERE r.rating IS NOT NULL OR CAST(:commented AS boolean)"
    "), "
    f"{album_latest('SELECT album_id FROM rated')}, "
    # The type and track count from the most recent play that has one, as the
    # album lists read them (``app.read.album_sql.ALBUM_PLAYS``).
    "rated_type AS ("
    "    SELECT DISTINCT ON (album_id) album_id, album_type FROM user_events "
    "    WHERE album_id IN (SELECT album_id FROM rated) AND album_type IS NOT NULL "
    "    ORDER BY album_id, occurred_at DESC, id DESC"
    "), "
    "rated_total AS ("
    "    SELECT DISTINCT ON (album_id) album_id, album_total_tracks AS total_tracks "
    "    FROM user_events "
    "    WHERE album_id IN (SELECT album_id FROM rated) AND album_total_tracks IS NOT NULL "
    "    ORDER BY album_id, occurred_at DESC, id DESC"
    ") "
    "SELECT rated.album_id, album_latest.name, album_latest.artists, "
    "       album_latest.artwork_url, rated.rating, rated.rated_at, "
    "       rated.comment, rated.commented_at, "
    "       CASE WHEN rated_type.album_type = 'single' "
    f"            THEN {release_kind('rated_total.total_tracks')} ELSE 'album' END AS release_type "
    "FROM rated LEFT JOIN album_latest USING (album_id) "
    "LEFT JOIN rated_type USING (album_id) LEFT JOIN rated_total USING (album_id) "
    "ORDER BY rated.rating DESC NULLS LAST, coalesce(rated.rated_at, rated.commented_at) DESC, "
    '         rated.album_id COLLATE "C"'
)


def read_all_ratings(
    conn: Connection, *, user_id: UUID, commented: bool = False
) -> list[RatedAlbum]:
    """Every rated release; with ``commented``, every release with only a comment too."""
    return [
        RatedAlbum(
            album_id=r.album_id,
            name=r.name,
            artists=list(r.artists or []),
            artwork_url=r.artwork_url,
            release_type=r.release_type,
            rating=r.rating,
            rated_at=r.rated_at,
            comment=r.comment,
            commented_at=r.commented_at,
        )
        for r in conn.execute(
            _ALL_RATINGS, {"user_id": user_id, "commented": commented, **_ALL_TIME}
        )
    ]
