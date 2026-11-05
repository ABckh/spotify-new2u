"""The artists shown for an album (WHO-128, WHO-135).

1. The album's own artists, as Spotify sends them (``track.album.artists``,
   stored as ``provider_extras.spotify_album_artists``), from the most recent
   stored play that has them. For a compilation such as *HELP(2)* that is the
   label, not the artists of whichever track was played last.
2. Rows stored before that key was collected and not yet backfilled have none.
   Then the album artists are the track artists found on **every** track of the
   album the user played, over the whole history, each track's artists being
   those of its most recent stored play (R-59): a single-artist album still
   shows its artist. Their order is their earliest position in a track's artist
   list, then the name by code point.
3. If no artist is on every track, ``[]``: the app shows "Unknown artist".
   Nothing is guessed.

``album_artists(among)`` is a set of CTEs ending in ``album_artists (album_id,
artists)``. It needs a ``user_events`` CTE in the same ``WITH`` with ``id``,
``item_key``, ``creators``, ``occurred_at``, ``album_id`` and ``album_artists``
(``provider_extras -> 'spotify_album_artists'``), already scoped to the user.
"""


def album_artists(among: str) -> str:
    """CTEs for the albums ``IN (among)``: a subquery, or ``:album_id`` for one album."""
    return f"""
    album_stored_artists AS (
        SELECT DISTINCT ON (album_id) album_id,
               ARRAY(SELECT jsonb_array_elements_text(album_artists)) AS artists
        FROM user_events
        WHERE album_id IN ({among}) AND album_artists IS NOT NULL
        ORDER BY album_id, occurred_at DESC, id DESC
    ),
    album_track_count AS (
        SELECT album_id, count(DISTINCT item_key) AS tracks
        FROM user_events WHERE album_id IN ({among}) GROUP BY album_id
    ),
    album_track_latest AS (
        SELECT DISTINCT ON (album_id, item_key) album_id, item_key, creators
        FROM user_events WHERE album_id IN ({among})
        ORDER BY album_id, item_key, occurred_at DESC, id DESC
    ),
    album_creator AS (
        SELECT t.album_id, c.name, min(c.position) AS position,
               count(DISTINCT t.item_key) AS tracks
        FROM album_track_latest t
        CROSS JOIN LATERAL unnest(t.creators) WITH ORDINALITY AS c(name, position)
        GROUP BY t.album_id, c.name
    ),
    album_shared_artists AS (
        SELECT album_id, array_agg(name ORDER BY position, name COLLATE "C") AS artists
        FROM album_creator JOIN album_track_count USING (album_id, tracks)
        GROUP BY album_id
    ),
    album_artists AS (
        SELECT t.album_id,
               COALESCE(s.artists, h.artists, ARRAY[]::text[]) AS artists
        FROM album_track_count t
        LEFT JOIN album_stored_artists s USING (album_id)
        LEFT JOIN album_shared_artists h USING (album_id)
    )
"""
