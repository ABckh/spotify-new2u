"""SQL every album read shares: "New to you", all played albums, and album tracks (WHO-133).

One copy of each rule, so a change to it reaches every list at once:

- ``ALBUM_EVENTS``: the ``user_events`` CTE, the user's Spotify plays that
  have an album, over the whole history, not only the period. An album is
  ``external_ids.spotify_album_id``. A play stored without one (the history
  file import, WHO-104, has track IDs and album names only) takes the album of
  the user's most recent play of the same track that has one, or else of their
  most recent play with the same album name and artist (both after NFC and
  trimming, R-31). With neither it has no album and is not in the CTE: every
  album read needs only plays with an album, and a history import stores most
  of its plays without one. A track with an album has it on every play, so its
  plays are all there. Resolved at read time, so a live play made later fills
  in older imported ones. ``ms`` is ``coalesce(played_ms, duration_ms, 0)``,
  as in the summary (R-25).
- ``ALBUM_PLAYS``: per album, when it became listened over the whole history
  and what was played of it in the period.
- ``ALBUM_IS_NEW``: when an album counts as new in the period (S14.2, WHO-123,
  WHO-130). Change it here only.
- ``ALBUM_LISTENED_IN_PERIOD``: when an album counts as listened in the period,
  so is in the played albums list (WHO-160). Change it here only.
- ``ALBUM_IS_LISTED``: whether a release is in an album list at all. A single
  (Spotify's ``album_type`` ``single``, EPs included) is not, in any album list
  or album count (WHO-159). Its tracks still count as songs. Singles and EPs
  have their own "New to you" list (``app.read.new_to_you``).
- ``release_kind``: ``single`` or ``ep`` for a release Spotify calls a single.
- ``album_latest``: the album name shown, that of the most recent stored play
  (R-59), the album's own artists (``app.read.album_artists``, WHO-128), and
  its cover, the most recent stored non-NULL ``artwork_url`` (WHO-149).
  Change any of them here only.
- ``track_artwork``: a track's cover, the same rule per ``item_key``.

Each needs the ``:user_id``, ``:start`` and ``:end`` parameters, and
``ALBUM_IS_NEW`` needs ``app.read.not_new.NOT_NEW`` in the same ``WITH``.
"""

from typing import Final

from app.read.album_artists import album_artists
from app.read.names import ARTIST_NAME, normalized_name

# The columns of ``user_events`` but ``album_id``, from a play ``e``.
_EVENT_COLUMNS: Final = """
    e.id, e.item_key, e.title, e.creators, e.collection, e.artwork_url, e.occurred_at,
    e.provider_extras -> 'spotify_album_artists' AS album_artists,
    (e.provider_extras ->> 'spotify_album_total_tracks')::numeric::int AS album_total_tracks,
    e.provider_extras ->> 'spotify_album_type' AS album_type,
    e.external_ids ->> 'isrc' AS isrc,
    COALESCE(e.played_ms, e.duration_ms, 0)::bigint AS ms,
    (e.occurred_at >= :start AND e.occurred_at < :end) AS in_period
"""

# The albums a play without one can take are built from the plays that have one
# (a few per day of polling), and the plays that take them are looked up in the
# indexes of migration 0017 rather than found among all plays: a history import
# stores ~100k plays without an album.
#
# - Plays with their own album: ``unified_event_spotify_album_idx``. Written as
#   ``->> ... IS NOT NULL``, which the planner can estimate from the migration's
#   statistics; for ``?`` it would guess, and read every play.
# - By track: ``unified_event_item_idx``. By name: ``unified_event_album_name_idx``,
#   whose expression is ``normalized_name("collection")`` exactly. Both ask for
#   ``= ANY (ARRAY(...))`` of the keys, so they are index lookups whatever the
#   planner guesses about the keys; a join alone may read every play instead
#   (``tests/test_album_read_indexes.py``).
#
# ``track_album`` and ``named_album`` hold at most one row per key, so no play is
# in ``user_events`` twice. A play's own album comes first; the track's applies
# only without one, and the name's only without either.
ALBUM_EVENTS: Final = f"""
    with_album AS (
        SELECT {_EVENT_COLUMNS}, e.external_ids ->> 'spotify_album_id' AS album_id
        FROM unified_event e
        WHERE e.user_id = :user_id AND e.source = 'spotify'
        AND e.external_ids ->> 'spotify_album_id' IS NOT NULL
    ),
    track_album AS (
        SELECT DISTINCT ON (item_key) item_key, album_id
        FROM with_album
        ORDER BY item_key, occurred_at DESC, id DESC
    ),
    named_album AS (
        SELECT DISTINCT ON (album_name, artist.name)
               {normalized_name("w.collection")} AS album_name, artist.name AS artist_name,
               w.album_id
        FROM with_album w
        CROSS JOIN LATERAL unnest(w.creators) AS creator(raw)
        CROSS JOIN LATERAL (SELECT {ARTIST_NAME} AS name) AS artist
        ORDER BY album_name, artist.name, w.occurred_at DESC, w.id DESC
    ),
    user_events AS (
        SELECT * FROM with_album
        UNION ALL
        SELECT {_EVENT_COLUMNS}, t.album_id
        FROM unified_event e JOIN track_album t ON t.item_key = e.item_key
        WHERE e.user_id = :user_id AND e.source = 'spotify'
        AND e.item_key = ANY (ARRAY(SELECT item_key FROM track_album))
        AND e.external_ids ->> 'spotify_album_id' IS NULL
        UNION ALL
        SELECT {_EVENT_COLUMNS}, n.album_id
        FROM unified_event e
        JOIN named_album n
            ON n.album_name = {normalized_name("e.collection")}
            AND n.artist_name = {normalized_name("e.creators[1]")}
        WHERE e.user_id = :user_id AND e.source = 'spotify'
        AND {normalized_name("e.collection")} = ANY (ARRAY(SELECT album_name FROM named_album))
        AND e.external_ids ->> 'spotify_album_id' IS NULL
        AND e.item_key NOT IN (SELECT item_key FROM track_album)
    )
"""

# An album is played when one of its tracks is (S14.3); the counts are of the
# period only.
#
# An album is *listened* once more than half of its distinct tracks were played,
# over the whole history (WHO-130): the play of its N-th distinct track, with
# N = floor(total / 2) + 1, so 10 tracks need 6, 7 need 4, 2 need 2, 1 needs 1.
# ``first_played_at`` is that play's time, and NULL while the album is not
# listened or its track count is unknown. A play before ``:end`` can only reach
# that count with plays before ``:end``, so "listened by the end of the period"
# is ``first_played_at < :end``.
#
# - ``album_track_first``: each distinct track's first play (``item_key``), so a
#   track played twice counts once.
# - ``album_total``: the track count from the most recent play that has one
#   (``provider_extras.spotify_album_total_tracks``, WHO-141), as R-59 does for names.
# - ``album_listened``: the N-th of those first plays in time order. Ties in time
#   give the same time whichever track is ranked first.
# - ``album_plays.tracks_listened``: the distinct tracks first played before
#   ``:end``, the count the rule compares (WHO-155). For a new album it is more
#   than half of ``total_tracks``. ``total_tracks`` is NULL while unknown.
# - ``album_type``: the release type from the most recent play that has one
#   (``provider_extras.spotify_album_type``, WHO-162), NULL while unknown.
ALBUM_PLAYS: Final = """
    album_track_first AS (
        SELECT album_id, item_key, min(occurred_at) AS first_at
        FROM user_events WHERE album_id IS NOT NULL GROUP BY album_id, item_key
    ),
    album_total AS (
        SELECT DISTINCT ON (album_id) album_id, album_total_tracks AS total_tracks
        FROM user_events
        WHERE album_id IS NOT NULL AND album_total_tracks IS NOT NULL
        ORDER BY album_id, occurred_at DESC, id DESC
    ),
    album_type AS (
        SELECT DISTINCT ON (album_id) album_id, album_type
        FROM user_events
        WHERE album_id IS NOT NULL AND album_type IS NOT NULL
        ORDER BY album_id, occurred_at DESC, id DESC
    ),
    album_listened AS (
        SELECT ranked.album_id, ranked.first_at AS listened_at
        FROM (
            SELECT album_id, first_at,
                   row_number() OVER (PARTITION BY album_id ORDER BY first_at, item_key) AS n
            FROM album_track_first
        ) AS ranked
        JOIN album_total USING (album_id)
        WHERE ranked.n = album_total.total_tracks / 2 + 1
    ),
    album_counts AS (
        SELECT album_id,
               count(*) FILTER (WHERE in_period) AS play_count,
               count(DISTINCT item_key) FILTER (WHERE in_period) AS tracks_played,
               COALESCE(sum(ms) FILTER (WHERE in_period), 0)::bigint AS total_ms
        FROM user_events WHERE album_id IS NOT NULL GROUP BY album_id
    ),
    album_tracks_by_end AS (
        SELECT album_id, count(*) FILTER (WHERE first_at < :end) AS tracks_listened
        FROM album_track_first GROUP BY album_id
    ),
    album_plays AS (
        SELECT album_counts.*, album_listened.listened_at AS first_played_at,
               album_tracks_by_end.tracks_listened, album_total.total_tracks,
               album_type.album_type
        FROM album_counts
        JOIN album_tracks_by_end USING (album_id)
        LEFT JOIN album_listened USING (album_id)
        LEFT JOIN album_total USING (album_id)
        LEFT JOIN album_type USING (album_id)
    )
"""

# A boolean over an ``album_plays`` row: listened in the period (more than half
# of its tracks played by the end of it, the last of them inside it), and not
# marked "Not new" by the user. An unknown track count gives NULL, so false.
ALBUM_IS_NEW: Final = (
    "COALESCE(album_plays.first_played_at >= :start AND album_plays.first_played_at < :end "
    "AND album_plays.album_id NOT IN (SELECT album_id FROM not_new_albums), false)"
)


# A boolean over an ``album_plays`` row: more than half of the album's tracks
# played in the period itself (WHO-160), with the same N as ``album_listened``:
# 10 tracks need 6, 7 need 4, 2 need 2, 1 needs 1. Plays before the period do
# not count; a track played twice counts once. An unknown track count gives
# NULL, so false.
ALBUM_LISTENED_IN_PERIOD: Final = (
    "COALESCE(album_plays.tracks_played > album_plays.total_tracks / 2.0, false)"
)

# A boolean over an ``album_plays`` row: false for a single (WHO-159). An unknown
# type is kept: the release is listed as an album until its type is known.
ALBUM_IS_LISTED: Final = "album_plays.album_type IS DISTINCT FROM 'single'"


# Spotify calls singles and EPs both ``single``. By its own guideline a single
# has 1 to 3 tracks and an EP 4 to 6, so more than 3 is an EP. ``total`` is SQL
# for the track count.
def release_kind(total: str) -> str:
    return f"CASE WHEN {total} > 3 THEN 'ep' ELSE 'single' END"


def album_latest(among: str) -> str:
    """CTEs ending in ``album_latest (album_id, name, artists, artwork_url)`` for the albums
    ``IN (among)``.

    ``among`` is SQL for the album IDs: a subquery, or ``:album_id`` for one album.
    """
    return f"""
    album_name AS (
        SELECT DISTINCT ON (album_id) album_id, collection AS name
        FROM user_events
        WHERE album_id IN ({among})
        ORDER BY album_id, occurred_at DESC, id DESC
    ),
    album_artwork AS (
        SELECT DISTINCT ON (album_id) album_id, artwork_url
        FROM user_events
        WHERE album_id IN ({among}) AND artwork_url IS NOT NULL
        ORDER BY album_id, occurred_at DESC, id DESC
    ),
    {album_artists(among)},
    album_latest AS (
        SELECT album_id, name, artists, album_artwork.artwork_url
        FROM album_name JOIN album_artists USING (album_id)
        LEFT JOIN album_artwork USING (album_id)
    )
"""


def track_artwork(events: str, among: str) -> str:
    """A CTE ``track_artwork (item_key, artwork_url)`` for the tracks ``IN (among)``.

    ``events`` is a CTE with ``id``, ``item_key``, ``occurred_at`` and
    ``artwork_url``. A track with no stored cover has no row: LEFT JOIN it.
    """
    return f"""
    track_artwork AS (
        SELECT DISTINCT ON (item_key) item_key, artwork_url
        FROM {events}
        WHERE item_key IN ({among}) AND artwork_url IS NOT NULL
        ORDER BY item_key, occurred_at DESC, id DESC
    )
"""
