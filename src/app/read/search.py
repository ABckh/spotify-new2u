"""Search: the user's albums and songs over the whole history, by name, artist or
the user's own comment on them.

- Every word of the query must be in the item, in any order and in any of its
  fields: an album's name, artists and the user's comment on it; a song's
  title, artists, the album name stored with its plays and the user's comment
  on it. A word matches a
  part of a field ("radio" finds "Radiohead"), compared after NFC in lower
  case (the database's ``lower``), so case never matters. A word never spans
  two fields.
- Albums are releases with an album ID, singles and EPs included, each with
  its ``release_type``, the user's rating and comment, and its plays over the
  whole history. Names, artists and covers follow the shared album rules
  (``app.read.album_sql``). A comment is always found by its words, even when
  the album's plays are gone; the album then has no name and no plays.
- Songs are ``item_key``, as everywhere else, with the title, artists, album
  name and cover of the most recent stored play (R-59), the album their plays
  resolve to (``ALBUM_EVENTS``), when there is one, and the user's comment. A
  song is matched by every name it was ever stored with.
- Each list is ordered most played first, then most recently played, then by
  ID, so the order is total. ``count`` is every match; ``limit`` cuts the items.
- Spotify plays only, like the album lists.

The query is the user's own text, and so are the names it matches: it is
never logged (the access log drops every query string) and never put in an
error message (PRD P12).

Every statement is scoped by ``user_id = :user_id`` from the session.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.read.album_sql import ALBUM_EVENTS, album_latest, release_kind, track_artwork
from app.read.ratings import ReleaseType

# In characters. A few words is what a search needs; the bound keeps the
# LIKE patterns small.
MAX_QUERY_LENGTH: Final = 200
MAX_WORDS: Final = 10
# Each list shows at most this many items; ``count`` says how many matched.
LIMIT: Final = 50

# ALBUM_EVENTS needs a period; search uses none of its period columns.
_ALL_TIME = {"start": datetime(1970, 1, 1, tzinfo=UTC), "end": datetime(1970, 1, 1, tzinfo=UTC)}


class SearchQueryError(ValueError):
    """A query that cannot be searched. ``message`` never holds the query."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def search_words(query: str | None) -> list[str]:
    """The query's words, split on whitespace, with LIKE's ``\\``, ``%`` and ``_``
    escaped. Raises ``SearchQueryError``.

    Python's whitespace includes ``chr(31)``, the separator ``_matches`` puts
    between fields, so no word can hold it and match across two fields.
    """
    if query is None or not query.strip():
        raise SearchQueryError("search_query_missing", "q is the text to search for.")
    if len(query) > MAX_QUERY_LENGTH or len(query.split()) > MAX_WORDS:
        raise SearchQueryError(
            "search_query_too_long",
            f"q is at most {MAX_QUERY_LENGTH} characters and {MAX_WORDS} words.",
        )
    if "\x00" in query:
        raise SearchQueryError("invalid_parameter", "q cannot hold NUL.")
    return [
        word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for word in query.split()
    ]


def _matches(fields: str) -> str:
    """SQL true when every word of ``:words`` is in one of ``fields`` (SQL, comma
    separated; a NULL field is skipped)."""
    patterns = (
        "ARRAY(SELECT '%' || lower(normalize(w, NFC)) || '%' "
        "FROM unnest(CAST(:words AS text[])) AS word(w))"
    )
    return f"lower(normalize(concat_ws(chr(31), {fields}), NFC)) LIKE ALL ({patterns})"


# Every album with a play, and every album with a comment, so a comment is
# found even without plays. The type and track count are those of the most
# recent play that has one, as in the album lists.
_ALBUMS = text(
    f"WITH {ALBUM_EVENTS}, "
    "commented AS (SELECT album_id, comment FROM album_comment WHERE user_id = :user_id), "
    "candidates AS (SELECT album_id FROM user_events UNION SELECT album_id FROM commented), "
    f"{album_latest('SELECT album_id FROM candidates')}, "
    "found AS ("
    "    SELECT c.album_id, l.name, COALESCE(l.artists, ARRAY[]::text[]) AS artists, "
    "           l.artwork_url, m.comment "
    "    FROM candidates c LEFT JOIN album_latest l USING (album_id) "
    "    LEFT JOIN commented m USING (album_id) "
    f"    WHERE {_matches('l.name, array_to_string(l.artists, chr(31)), m.comment')}"
    "), "
    "found_plays AS ("
    "    SELECT album_id, count(*) AS play_count, max(occurred_at) AS last_played_at "
    "    FROM user_events WHERE album_id IN (SELECT album_id FROM found) GROUP BY album_id"
    "), "
    "found_type AS ("
    "    SELECT DISTINCT ON (album_id) album_id, album_type FROM user_events "
    "    WHERE album_id IN (SELECT album_id FROM found) AND album_type IS NOT NULL "
    "    ORDER BY album_id, occurred_at DESC, id DESC"
    "), "
    "found_total AS ("
    "    SELECT DISTINCT ON (album_id) album_id, album_total_tracks AS total_tracks "
    "    FROM user_events "
    "    WHERE album_id IN (SELECT album_id FROM found) AND album_total_tracks IS NOT NULL "
    "    ORDER BY album_id, occurred_at DESC, id DESC"
    ") "
    "SELECT found.album_id, found.name, found.artists, found.artwork_url, found.comment, "
    "       r.rating, COALESCE(p.play_count, 0) AS play_count, p.last_played_at, "
    "       CASE WHEN t.album_type = 'single' "
    f"            THEN {release_kind('n.total_tracks')} ELSE 'album' END AS release_type, "
    "       count(*) OVER () AS total "
    "FROM found LEFT JOIN found_plays p USING (album_id) "
    "LEFT JOIN found_type t USING (album_id) LEFT JOIN found_total n USING (album_id) "
    "LEFT JOIN album_rating r ON r.user_id = :user_id AND r.album_id = found.album_id "
    "ORDER BY play_count DESC, p.last_played_at DESC NULLS LAST, "
    'found.album_id COLLATE "C" '
    "LIMIT :limit"
)

# One pass over the plays: each stored name of a song (title, artists, album
# name) with its plays, so a song played 100 times is matched once per name.
# A song is found when any of its names matches, and its counts are those of
# all its names. Names, cover and album are then read only for the songs shown.
_TRACKS = text(
    f"WITH {ALBUM_EVENTS}, "
    "spotify_events AS NOT MATERIALIZED ("
    "    SELECT id, item_key, title, creators, collection, artwork_url, occurred_at "
    "    FROM unified_event WHERE user_id = :user_id AND source = 'spotify'"
    "), "
    "named AS ("
    "    SELECT item_key, title, creators, collection, count(*) AS play_count, "
    "           min(occurred_at) AS first_played_at, max(occurred_at) AS last_played_at "
    "    FROM spotify_events GROUP BY item_key, title, creators, collection"
    "), "
    "found AS ("
    "    SELECT n.item_key, sum(n.play_count)::bigint AS play_count, "
    "           min(n.first_played_at) AS first_played_at, "
    "           max(n.last_played_at) AS last_played_at "
    "    FROM named n "
    "    LEFT JOIN track_comment c ON c.user_id = :user_id AND c.item_key = n.item_key "
    "    GROUP BY n.item_key "
    "    HAVING bool_or("
    f"        {_matches('n.title, array_to_string(n.creators, chr(31)), n.collection, c.comment')}"
    "    )"
    "), "
    "shown AS ("
    "    SELECT *, count(*) OVER () AS total FROM found "
    '    ORDER BY play_count DESC, last_played_at DESC, item_key COLLATE "C" '
    "    LIMIT :limit"
    "), "
    "latest AS ("
    "    SELECT DISTINCT ON (item_key) item_key, title, creators, collection FROM spotify_events "
    "    WHERE item_key IN (SELECT item_key FROM shown) "
    "    ORDER BY item_key, occurred_at DESC, id DESC"
    "), "
    f"{track_artwork('spotify_events', 'SELECT item_key FROM shown')}, "
    "song_album AS ("
    "    SELECT DISTINCT ON (item_key) item_key, album_id FROM user_events "
    "    WHERE item_key IN (SELECT item_key FROM shown) "
    "    ORDER BY item_key, occurred_at DESC, id DESC"
    ") "
    "SELECT shown.item_key, latest.title, latest.creators, latest.collection AS album, "
    "       song_album.album_id, track_artwork.artwork_url, c.comment, play_count, "
    "       first_played_at, last_played_at, total "
    "FROM shown JOIN latest USING (item_key) LEFT JOIN track_artwork USING (item_key) "
    "LEFT JOIN song_album USING (item_key) "
    "LEFT JOIN track_comment c ON c.user_id = :user_id AND c.item_key = shown.item_key "
    'ORDER BY play_count DESC, last_played_at DESC, shown.item_key COLLATE "C"'
)


@dataclass(frozen=True)
class FoundAlbum:
    album_id: str
    # None only for an album found by its comment whose plays are gone.
    name: str | None
    artists: list[str]
    artwork_url: str | None
    release_type: ReleaseType
    rating: int | None
    comment: str | None
    play_count: int
    last_played_at: datetime | None
    last_played_local_date: date | None  # in the display zone


@dataclass(frozen=True)
class FoundTrack:
    item_key: str
    title: str
    creators: list[str]
    album: str | None  # the album name stored with the most recent play
    album_id: str | None  # the album the song's plays resolve to, if any
    artwork_url: str | None
    comment: str | None  # the user's own comment on the track
    play_count: int
    first_played_at: datetime
    last_played_at: datetime
    last_played_local_date: date  # in the display zone


@dataclass(frozen=True)
class SearchResult:
    albums: list[FoundAlbum]
    album_count: int
    tracks: list[FoundTrack]
    track_count: int


def read_search(
    conn: Connection, *, user_id: UUID, words: list[str], time_zone: str, limit: int = LIMIT
) -> SearchResult:
    """``words`` from ``search_words``; ``time_zone`` is the display zone of the local dates."""
    zone = ZoneInfo(time_zone)
    params = {"user_id": user_id, "words": words, "limit": limit, **_ALL_TIME}
    albums: list[FoundAlbum] = []
    tracks: list[FoundTrack] = []
    album_count = track_count = 0
    for r in conn.execute(_ALBUMS, params):
        album_count = r.total
        albums.append(
            FoundAlbum(
                album_id=r.album_id,
                name=r.name,
                artists=list(r.artists),
                artwork_url=r.artwork_url,
                release_type=r.release_type,
                rating=r.rating,
                comment=r.comment,
                play_count=r.play_count,
                last_played_at=r.last_played_at,
                last_played_local_date=(
                    r.last_played_at.astimezone(zone).date() if r.last_played_at else None
                ),
            )
        )
    for r in conn.execute(_TRACKS, params):
        track_count = r.total
        tracks.append(
            FoundTrack(
                item_key=r.item_key,
                title=r.title,
                creators=list(r.creators),
                album=r.album,
                album_id=r.album_id,
                artwork_url=r.artwork_url,
                comment=r.comment,
                play_count=r.play_count,
                first_played_at=r.first_played_at,
                last_played_at=r.last_played_at,
                last_played_local_date=r.last_played_at.astimezone(zone).date(),
            )
        )
    return SearchResult(albums, album_count, tracks, track_count)
