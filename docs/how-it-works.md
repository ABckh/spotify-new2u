# How it works

## Sign-in and collection

Sign-in (details in `src/app/auth/login.py`; routes in `src/app/api/auth.py`):

1. App: `POST /auth/spotify/login {"home_timezone": "<IANA zone>"}` ->
   `login_id`, `claim_secret`, `authorize_url`. The app opens `authorize_url`
   in `ASWebAuthenticationSession`. Spotify always shows its consent screen
   (`show_dialog=true`). A first connect always imports the latest 50 plays
   Spotify lists; a renewal never does.
   An `import_recent_history` field from older app builds is ignored.
2. Spotify redirects the browser to `/auth/spotify/callback`. The server checks
   `state` (accepted once, 10-minute expiry), exchanges the code with the PKCE
   verifier, keeps the refresh token encrypted, and creates or updates the user.
   It then `303`-redirects to `APP_CALLBACK_URL` with `login_id`, `result` and
   a one-time `callback_code`.
3. App: `POST /auth/spotify/login/{login_id}/claim {"claim_secret", "callback_code"}`
   -> `202` while pending, then `200 {"session_token", "expires_at",
   "refresh_token", "refresh_expires_at"}` exactly once. Both values are
   required: the callback code binds the session to the device whose browser
   finished the sign-in.
4. App: `Authorization: Bearer <session_token>` on every call. The session token
   lasts `SESSION_TOKEN_TTL_SECONDS` (an hour). When a call answers `401`, the
   app sends `POST /auth/refresh {"refresh_token"}` and gets the same four
   fields back: a new session token and a **new** refresh token (rotation; each
   refresh token works once). It then retries the call. The refresh token lasts
   `REFRESH_TOKEN_TTL_SECONDS` (90 days) from its last use, so the user signs in
   with Spotify again only after that long without opening the app. A refresh
   token presented a second time means it leaked, so the whole session is
   revoked (reuse detection). Both are stored only as keyed hashes. `POST
   /auth/logout` revokes the session and its refresh token. `GET /v1/me/connection` reports the connection: `status`
   (`active` | `reauth_required`), `last_polled_at` (last successful poll),
   `last_poll_attempt_at` (newest poll attempt, scheduled or manual, failed or
   not), `next_manual_poll_at` (when a manual poll is next accepted; `null` or
   in the past means now), `grant_expires_at`, `reconnect_soon`,
   `poll_interval_seconds`, `collection_started_at` (where
   live collection begins), `history_imported_at` (when the recent-history
   import ran; `null` if not done yet) and `has_imported_plays`
   (`true` only if the import stored a play from before `collection_started_at`).
   Draw the "imported history ends / live collection begins" marker only when
   `has_imported_plays` is `true`, never from `history_imported_at` alone.
   Plays from the extended history file import (step 6) count for it too.
5. App: `POST /v1/me/spotify/poll` asks for a poll now. `202
   {"requested_at", "next_allowed_at"}`: the poller starts it within
   `POLLER_NEW_CONNECTION_CHECK_SECONDS`, so reload after about 20 s. `409`
   `no_active_connection` when there is no active connection. `429` with
   `Retry-After` (whole seconds) when the last poll attempt or manual request
   was less than `MANUAL_POLL_COOLDOWN_SECONDS` (300) ago; the cooldown is kept
   in the database, so restarts do not reset it.
6. App: `POST /v1/spotify/history-import`, `multipart/form-data` with one part
   `file`: the `my_spotify_data.zip` Spotify emails (or one
   `Streaming_History_Audio_*.json` from it), as is. `202
   {"id", "status": "queued"}`; the poller's import worker runs it within
   `HISTORY_IMPORT_CHECK_SECONDS` (5). `GET /v1/spotify/history-import` ->
   `{"latest": null}` or the latest import's `status` (`queued` | `running` |
   `succeeded` | `failed`), `error_kind` and `counts`; poll it while the import
   runs. The import is one time per user: `409 history_import_in_progress`
   while one is queued or running, `409 history_import_already_done` after one
   succeeded; after a `failed` one the user may upload again, and the retry
   adds no duplicates. `413 upload_too_large` over 200 MB; `415
   not_multipart`; `422` with a code for a file that is not a Spotify export or
   breaks a zip limit (the full list is in `app/api/history_import.py`). Only
   music is kept: podcasts, audiobooks, videos, private-session plays and plays
   under 30 s are skipped. Of each play, only the time, the track id, the
   track, artist and album names and the time played are stored; the upload
   file is deleted when the job ends.

Collection: the poller polls every active connection every
`POLLER_INTERVAL_SECONDS` (900, 15 minutes). A connection
not yet polled since its user connected is polled within
`POLLER_NEW_CONNECTION_CHECK_SECONDS` (15) instead, and so is a
connection whose user asked for a poll now and has had no poll attempt since.
If the user chose the history import at their first connect, that
first poll also stores the latest 50 plays Spotify lists, whenever they were
made, as one `api_backfill` batch, once; every other poll stores only plays
from `collection_started_at` on. Each play is
one `unified_event` row; a play already stored is a no-op, so overlapping polls
add nothing. Spotify refresh tokens expire 6 months after authorization; the
poller logs a warning from 30 days before (`REAUTH_WARNING_DAYS`) and, on
`invalid_grant`, marks the connection `reauth_required` and stops polling it
until the user reconnects.

Known-music baseline: sign-in also asks for the read-only scopes
`user-top-read`, `user-library-read` and `user-follow-read`. Once per connection,
right after the first poll's history import, the poller reads the user's saved
albums and songs (added before collection started), followed artists and top
lists, and stores them as "Not new" marks with `source = 'spotify_library'`
(`src/app/spotify/library_baseline.py`). They hide an item exactly like the
user's own mark, and Undo removes them. At most 86 Spotify calls per baseline.
A grant from before these scopes gets no baseline until the user signs in again.

First-two-weeks review: `GET /v1/me/review` lists the albums, singles and EPs
first played from the earliest stored play to 14 days after collection started that
are still new; the app offers it as "Did you already know these?". The user
marks the ones they knew with `POST /v1/new-to-you/not-new
{"items": [{"kind", "item_id"}, ...]}` (at most 200; `200 {"marked",
"not_played"}`) and finishes with `POST /v1/me/review/done {"through"}`, which
keeps the rest new. The review closes for good 14 days after its window ends
(`src/app/read/review.py`).

Album ratings: `PUT /v1/albums/{album_id}/rating {"rating": 1..10}` rates an
album, single or EP the user played (`404` otherwise, `422` for anything but a
whole number from 1 to 10); `DELETE` clears it. Every album item in the read API
carries `rating` (the user's, or `null`), and `GET /v1/ratings/albums` lists
everything rated, all time, best first, each with `release_type` `album`,
`single` or `ep` (`src/app/read/ratings.py`, migration 0015). The app groups
them into five leagues by score: 10, 9, 8, 6–7 and 1–5.

Album comments: `PUT /v1/albums/{album_id}/comment {"comment": "..."}` stores
the user's own words about an album, single or EP they played (`404`
otherwise): 1 to 2000 characters, line breaks allowed, not blank, no NUL
(`422` with one fixed message, which never repeats the text). `DELETE` clears
it. A comment and a rating are independent: either can exist alone, and
setting or clearing one never changes the other (migration 0016 keeps them in
separate tables). Every album item carries `comment` (the user's, or `null`)
next to `rating`, and so does each item of `GET /v1/ratings/albums`.
`GET /v1/ratings/albums?commented=true` also lists, after every rated release,
the ones with only a comment (`rating` `null`), most recently commented
first; without it the list is as before, so older app builds keep working.
A comment is never logged and never on the share picture; it is in the data
export and goes with `DELETE /v1/me`.

Track comments: `PUT /v1/tracks/{item_key}/comment {"comment": "..."}` stores
the user's own words about a track they played, by the same rules as an album
comment (`404` for a track with no play, `422` for a key that is not a track
`item_key`); `DELETE` clears it (migration 0018, `track_comment`). Every play
of `GET /v1/timeline`, every track of an album screen and every song of
`GET /v1/search` carries `comment`, and search finds a song by its comment.

All singles and EPs: `GET /v1/singles` answers like `GET /v1/albums`, for the
singles and EPs listened in the period (more than half of their tracks played
in it), each with `release_type` `single` or `ep`. Every `/v1/albums` item has
`release_type` `album`.

Search: `GET /v1/search?q=...` finds the user's albums (singles and EPs
included) and songs over the whole history that hold every word of `q`, in any
order, in any case, and as part of a word ("radio" finds "Radiohead"). An album
is matched by its name, its artists and the user's comment on it, so a comment
finds its album and an album shows its comment; a song by its title, artists
and the album name stored with its plays. Albums carry `release_type`,
`rating`, `comment`, `play_count` and `last_played_at`; songs carry `album`,
`album_id` when their plays resolve to one, `play_count` and their first and
last play. Each list is most played first, at most 50 items, with `count`.
`q` is 1 to 200 characters and at most 10 words (`422` otherwise), and like
every query string it is never logged (`src/app/read/search.py`).

New singles and EPs: `GET /v1/new-to-you` has a `singles` list next to
`albums`. Spotify calls both `single`; 1–3 tracks is a single and more is an EP
(`release_type`). They are new by the album rule (more than half the tracks
played), and a single whose every played song is also on an album new in the
same period is left out, so the song shows with its album only (songs match by
ISRC, or by title and first artist, ignoring case).

Artists: there is no artist list any more. `/v1/artists`, the artist lists in
"New to you" and the review, and artist "Not new" marks were removed; the code
is on the `archive/artists` branch (tag `archive-artists-2026-10-29`).
