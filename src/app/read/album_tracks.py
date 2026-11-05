"""The tracks of one album that the user played in one period (WHO-116, WHO-115).

The app opens this list when the user taps an album under "New to you" or in
the list of every album played in the period (WHO-133).

- An album is a Spotify album ID (``external_ids.spotify_album_id``), as in
  ``app.read.new_to_you``. A track is ``item_key``.
- Only tracks with at least one play of this album in the period are listed.
  ``play_count`` counts those plays.
- ``is_new`` and ``first_played_*`` use the track's earliest stored play for
  this user over the whole history and over every album, the same rule as the
  "New to you" track list (S14.2). So a track is new here exactly when it is
  new there.
- A track marked "Not new", or on an album marked "Not new", is still listed
  if played, with ``is_new`` false (WHO-123, ``app.read.not_new``).
- Track title and creators, and the album name, are those of the most recent
  stored play (R-59). The album artists are the album's own, as in "New to
  you" (``app.read.album_artists``, WHO-128).
- ``artwork_url`` is the album's cover (``app.read.album_sql``), on the album
  and on each of its tracks (WHO-149).
- Order: ``play_count`` descending, then the title after NFC and trimming by
  code point, then ``item_key``, so the order never depends on the planner (R-60).
- An album the user never played in the period gives an empty list. Album
  ``name`` and ``artists`` come only from this user's plays, so an album that
  only another user played reads as unknown: ``null`` and ``[]``.
- Spotify plays only, as the rest of "New to you" (S14.18).

Every statement is scoped by ``user_id = :user_id`` from the session.
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.read.album_sql import ALBUM_EVENTS, album_latest
from app.read.names import normalized_name
from app.read.not_new import NOT_NEW
from app.read.periods import Period

_TRACKS = text(
    f"WITH {ALBUM_EVENTS}, {NOT_NEW}, "
    "played AS ("
    "    SELECT item_key, count(*) AS play_count FROM user_events "
    "    WHERE album_id = :album_id AND in_period GROUP BY item_key"
    "), "
    "first_play AS ("
    "    SELECT item_key, min(occurred_at) AS first_played_at FROM user_events "
    "    WHERE item_key IN (SELECT item_key FROM played) GROUP BY item_key"
    "), "
    "latest AS ("
    "    SELECT DISTINCT ON (item_key) item_key, title, creators FROM user_events "
    "    WHERE item_key IN (SELECT item_key FROM played) "
    "    ORDER BY item_key, occurred_at DESC, id DESC"
    ") "
    "SELECT item_key, title, creators, play_count, first_played_at, "
    "       item_key IN (SELECT item_key FROM not_new_tracks) AS marked_not_new "
    "FROM played JOIN first_play USING (item_key) JOIN latest USING (item_key) "
    f'ORDER BY play_count DESC, {normalized_name("title")} COLLATE "C", item_key COLLATE "C"'
)

# The same album name and artists as every album list (``app.read.album_sql``).
_ALBUM = text(
    f"WITH {ALBUM_EVENTS}, {album_latest(':album_id')} "
    "SELECT name, artists, artwork_url FROM album_latest"
)


@dataclass(frozen=True)
class AlbumTrack:
    item_key: str
    title: str
    creators: list[str]
    artwork_url: str | None
    play_count: int
    first_played_at: datetime
    first_played_local_date: date
    is_new: bool


@dataclass(frozen=True)
class AlbumTracks:
    album_id: str
    name: str | None
    artists: list[str]
    artwork_url: str | None
    tracks: list[AlbumTrack]


def read_album_tracks(
    conn: Connection, *, user_id: UUID, album_id: str, period: Period
) -> AlbumTracks:
    zone = ZoneInfo(period.time_zone)
    params = {"user_id": user_id, "album_id": album_id, "start": period.start, "end": period.end}
    album = conn.execute(_ALBUM, params).one_or_none()
    artwork_url = album.artwork_url if album else None
    tracks = [
        AlbumTrack(
            item_key=r.item_key,
            title=r.title,
            creators=list(r.creators),
            artwork_url=artwork_url,
            play_count=r.play_count,
            first_played_at=r.first_played_at,
            first_played_local_date=r.first_played_at.astimezone(zone).date(),
            is_new=not r.marked_not_new and period.start <= r.first_played_at < period.end,
        )
        for r in conn.execute(_TRACKS, params)
    ]
    return AlbumTracks(
        album_id=album_id,
        name=album.name if album else None,
        artists=list(album.artists) if album else [],
        artwork_url=artwork_url,
        tracks=tracks,
    )
