"""Every stored field, in one constant.

The "what we collect" screen (PRD F9, story S6) renders its exact field list from
``STORED_FIELDS``. ``tests/test_privacy_stored_fields.py`` enumerates the live
database columns and fails when any column is in neither ``STORED_FIELDS`` nor
``NOT_USER_DATA``, so a new column cannot become undisclosed silently.

Each field belongs to exactly one ``Category``. The categories are the plain-
language lines of fixed string W2 on WHO-15; a field that fits none of them
means Product adds a line, not that the field goes unlisted.

The entries for ``unified_event`` and ``ingest_batch`` follow schema v1 on WHO-2
(``search_tsv`` dropped by CTO decision on WHO-17). ``spotify_connection`` lists
the profile and collection-start fields (WHO-29); ``app_user.home_timezone``
follows schema revision 3 (WHO-23). The token, liveness and session columns came
with the WHO-6 migration that created those tables. ``collection_gap`` came with
migration 0003 (WHO-44) under the W2 line "Gaps in collection" (WHO-43, WHO-47).
``spotify_renewal`` came with migration 0005 (WHO-61). The manual-poll request
time on ``spotify_connection`` came with migration 0006 (WHO-95). The
history-import choice and its completion time came with migration 0007
(WHO-89); imported plays are ordinary ``unified_event`` rows with
``ingest_method = 'api_backfill'``. ``not_new_mark`` came with migration 0008
(WHO-124) under the W2 line "Your settings": what the user chose to hide from
"New to you". Its ``source`` and the connection's ``library_baseline_at`` and
``review_cleared_at`` came with migration 0013 (the known-music baseline and
the first-two-weeks review).
``history_import`` came with migration 0014
(WHO-104): the one-time import of the Spotify extended streaming history file,
whose plays are ``unified_event`` rows with ``source_artifact =
'spotify_extended_history'`` and ``ingest_method = 'file_import'``. Of that file
only the play time, track id, track, artist and album names and the time played
are kept; nothing of the file itself is stored.
``album_rating`` came with migration 0015 under "Your settings": the user's own
album ratings. ``album_comment`` came with migration 0016, next to it: the
user's own comments on albums, in their words, never logged. ``track_comment``
came with migration 0018: the same, on tracks.
``unified_event.provider_extras`` keys are listed from ``PROVIDER_EXTRAS_ALLOWLIST``;
``spotify_album_total_tracks`` came with migration 0010 (WHO-141), ``spotify_album_type``
with migration 0011 (WHO-162).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Category(StrEnum):
    """One line of fixed string W2 each."""

    PLAYS = "plays"
    TRACK_DETAILS = "track_details"
    COLLECTION_RECORDS = "collection_records"
    SETTINGS = "settings"
    CONNECTION = "connection"
    COLLECTION_GAPS = "collection_gaps"


@dataclass(frozen=True)
class StoredField:
    table: str
    column: str
    category: Category
    label: str


def _fields(table: str, *rows: tuple[str, Category, str]) -> tuple[StoredField, ...]:
    return tuple(StoredField(table, column, category, label) for column, category, label in rows)


STORED_FIELDS: Final[tuple[StoredField, ...]] = (
    *_fields(
        "unified_event",
        ("id", Category.PLAYS, "Our identifier for each play"),
        ("user_id", Category.PLAYS, "Which account each play belongs to"),
        ("source", Category.PLAYS, "Where the play came from, for example Spotify"),
        ("kind", Category.PLAYS, "Whether it was listened to or watched"),
        ("occurred_at", Category.PLAYS, "When you played it"),
        ("occurred_at_precision", Category.PLAYS, "Whether we know the exact time or only the day"),
        ("duration_ms", Category.PLAYS, "How long the track is"),
        ("played_ms", Category.PLAYS, "How much was played, when the source reports it"),
        ("item_key", Category.PLAYS, "The identifier we use to recognise the same item again"),
        (
            "item_key_source",
            Category.PLAYS,
            "Whether that identifier came from the source or the title",
        ),
        ("dedupe_key", Category.PLAYS, "A fingerprint of each play, so it is never stored twice"),
        (
            "day_key",
            Category.PLAYS,
            "A fingerprint of the play's day, so imports do not double-count",
        ),
        ("dedupe_key_version", Category.PLAYS, "Which fingerprint recipe was used"),
        ("title", Category.TRACK_DETAILS, "The track or video title"),
        ("creators", Category.TRACK_DETAILS, "The artists or channel"),
        ("collection", Category.TRACK_DETAILS, "The album, series or show"),
        ("external_ids", Category.TRACK_DETAILS, "The source's identifiers for the item"),
        ("artwork_url", Category.TRACK_DETAILS, "A link to the cover art, which may expire"),
        ("provider_extras", Category.TRACK_DETAILS, "Extra item details, each listed below"),
        (
            "source_artifact",
            Category.COLLECTION_RECORDS,
            "Which Spotify feature or file the play came from",
        ),
        (
            "ingest_method",
            Category.COLLECTION_RECORDS,
            "Whether we collected it, imported it from your recent Spotify plays, "
            "or you imported it from a file",
        ),
        ("ingest_batch_id", Category.COLLECTION_RECORDS, "Which collection run stored the play"),
        ("ingested_at", Category.COLLECTION_RECORDS, "When we stored the play"),
    ),
    *_fields(
        "ingest_batch",
        ("id", Category.COLLECTION_RECORDS, "Our identifier for each collection run"),
        ("user_id", Category.COLLECTION_RECORDS, "Which account the run was for"),
        ("source", Category.COLLECTION_RECORDS, "Which source we checked"),
        ("source_artifact", Category.COLLECTION_RECORDS, "Which Spotify feature or file we read"),
        ("ingest_method", Category.COLLECTION_RECORDS, "Whether it was a collection or an import"),
        ("started_at", Category.COLLECTION_RECORDS, "When the run started"),
        ("finished_at", Category.COLLECTION_RECORDS, "When the run finished"),
        ("window_start", Category.COLLECTION_RECORDS, "The earliest play time the run covered"),
        ("window_end", Category.COLLECTION_RECORDS, "The latest play time the run covered"),
        ("rows_seen", Category.COLLECTION_RECORDS, "How many plays the source returned"),
        ("rows_inserted", Category.COLLECTION_RECORDS, "How many of them were new"),
        (
            "rows_upgraded",
            Category.COLLECTION_RECORDS,
            "How many made an earlier play more precise",
        ),
        ("rows_skipped", Category.COLLECTION_RECORDS, "How many we already had"),
        ("status", Category.COLLECTION_RECORDS, "Whether the run succeeded"),
        (
            "error_kind",
            Category.COLLECTION_RECORDS,
            "The kind of error, if it failed, never the error text",
        ),
    ),
    # W2 "Your settings". Schema rev 3 (WHO-23): the IANA zone, set once at signup,
    # that decides which date a Netflix day-only row and its exact-time row share.
    *_fields(
        "app_user",
        ("id", Category.SETTINGS, "Our identifier for your account"),
        (
            "home_timezone",
            Category.SETTINGS,
            "Your home time zone, so imported viewing lands on the right day",
        ),
    ),
    # W2 "Your connection". From the Spotify profile only the account ID and the
    # display name are kept (PRD P8, S1.4a); app.spotify.profile reads nothing
    # else. The refresh token is stored encrypted (WHO-6).
    *_fields(
        "spotify_connection",
        ("user_id", Category.CONNECTION, "Which account the Spotify connection belongs to"),
        ("spotify_account_id", Category.CONNECTION, "Your Spotify account ID"),
        ("spotify_display_name", Category.CONNECTION, "Your Spotify display name"),
        (
            "collection_started_at",
            Category.CONNECTION,
            "When collection started: plays from before it are stored only if we "
            "imported them from your recent Spotify plays or your Spotify history file",
        ),
        (
            "authorized_at",
            Category.CONNECTION,
            "When you last allowed access, so we can warn you before it runs out",
        ),
        (
            "refresh_token_ciphertext",
            Category.CONNECTION,
            "Spotify's access key for your account, encrypted, never sent to the app",
        ),
        ("status", Category.CONNECTION, "Whether collection is working or needs you to reconnect"),
        ("reauth_reason", Category.CONNECTION, "Why you need to reconnect, if you do"),
        ("last_polled_at", Category.CONNECTION, "When we last checked Spotify successfully"),
        (
            "history_import_requested",
            Category.CONNECTION,
            "Whether we import your recent Spotify plays when you first connect. Always "
            "yes for new accounts; accounts that connected before this was automatic "
            "may say no",
        ),
        (
            "history_imported_at",
            Category.CONNECTION,
            "When we imported them, empty if we have not",
        ),
        ("poll_requested_at", Category.CONNECTION, "When you last asked us to check Spotify now"),
        (
            "library_baseline_at",
            Category.CONNECTION,
            "When we read what your Spotify library says you already know, empty if we have not",
        ),
        (
            "review_cleared_at",
            Category.SETTINGS,
            'How far you have gone through "Did you already know these?", empty if not yet',
        ),
    ),
    # W2 "Your settings": albums, artists and songs you marked "Not new" (WHO-123).
    *_fields(
        "not_new_mark",
        ("user_id", Category.SETTINGS, 'Which account each "Not new" mark belongs to'),
        (
            "kind",
            Category.SETTINGS,
            'Whether you marked an album, an artist or a song as "Not new"',
        ),
        (
            "item_id",
            Category.SETTINGS,
            "Which album, artist or song you marked: the album's Spotify ID, "
            "the artist's name or our identifier for the song",
        ),
        ("created_at", Category.SETTINGS, "When you marked it"),
        (
            "source",
            Category.SETTINGS,
            "Whether you marked it, or we did because it was in your saved albums, saved "
            "songs, followed artists or Spotify top lists before you connected",
        ),
    ),
    # The one-time import of the Spotify extended streaming history (WHO-104):
    # its state and what it did with the rows of the file. No file name, path
    # or content. CONNECTION, not COLLECTION_RECORDS: the row is kept until the
    # user deletes, because it enforces the one-time rule, while collection
    # records are kept 90 days (WHO-103 §6.1). Labels from WHO-103 §6.5.
    *_fields(
        "history_import",
        ("id", Category.CONNECTION, "Our identifier for your Spotify history file import"),
        ("user_id", Category.CONNECTION, "Which account the import was for"),
        ("file_kind", Category.CONNECTION, "Whether you uploaded a zip or a JSON file"),
        (
            "status",
            Category.CONNECTION,
            "Whether your Spotify history file import is waiting, running, finished or failed",
        ),
        ("error_kind", Category.CONNECTION, "Why the import failed, if it did"),
        ("created_at", Category.CONNECTION, "When you uploaded the file"),
        ("started_at", Category.CONNECTION, "When the import started"),
        ("finished_at", Category.CONNECTION, "When the import ended"),
        ("ingest_batch_id", Category.CONNECTION, "Which collection run stored its plays"),
        ("rows_seen", Category.CONNECTION, "How many plays the import found in the file"),
        ("rows_inserted", Category.CONNECTION, "How many plays the import added"),
        ("rows_duplicate", Category.CONNECTION, "How many plays the import found already here"),
        (
            "rows_skipped_not_music",
            Category.CONNECTION,
            "How many plays the import skipped because they were podcasts, audiobooks or videos",
        ),
        (
            "rows_skipped_incognito",
            Category.CONNECTION,
            "How many plays the import skipped because they were from a private session",
        ),
        (
            "rows_skipped_short",
            Category.CONNECTION,
            "How many plays the import skipped because they were shorter than 30 seconds",
        ),
        (
            "rows_unreadable",
            Category.CONNECTION,
            "How many plays the import skipped because we could not read them",
        ),
    ),
    # W2 "Your settings": your own ratings of albums, 1 to 10.
    *_fields(
        "album_rating",
        ("user_id", Category.SETTINGS, "Which account each album rating belongs to"),
        ("album_id", Category.SETTINGS, "Which album you rated: its Spotify ID"),
        ("rating", Category.SETTINGS, "Your rating of the album, from 1 to 10"),
        ("rated_at", Category.SETTINGS, "When you last rated it"),
    ),
    # W2 "Your settings": your own comments on albums, apart from the ratings.
    *_fields(
        "album_comment",
        ("user_id", Category.SETTINGS, "Which account each album comment belongs to"),
        ("album_id", Category.SETTINGS, "Which album you commented on: its Spotify ID"),
        ("comment", Category.SETTINGS, "Your comment on the album, in your own words"),
        ("commented_at", Category.SETTINGS, "When you last changed the comment"),
    ),
    # W2 "Your settings": your own comments on tracks.
    *_fields(
        "track_comment",
        ("user_id", Category.SETTINGS, "Which account each track comment belongs to"),
        ("item_key", Category.SETTINGS, "Which track you commented on: its Spotify ID"),
        ("comment", Category.SETTINGS, "Your comment on the track, in your own words"),
        ("commented_at", Category.SETTINGS, "When you last changed the comment"),
    ),
    # Sign-in sessions of this app. Only a keyed hash of each secret is stored.
    *_fields(
        "auth_session",
        ("id", Category.CONNECTION, "Our identifier for each signed-in session"),
        ("user_id", Category.CONNECTION, "Which account the session belongs to"),
        ("login_id", Category.CONNECTION, "Which sign-in created the session"),
        (
            "claim_secret_hash",
            Category.CONNECTION,
            "A fingerprint of the one-time sign-in secret, until it is used",
        ),
        ("token_hash", Category.CONNECTION, "A fingerprint of the session key, never the key"),
        ("created_at", Category.CONNECTION, "When you signed in"),
        ("expires_at", Category.CONNECTION, "When the session key stops working"),
        (
            "refresh_token_hash",
            Category.CONNECTION,
            "A fingerprint of the key that keeps you signed in, never the key",
        ),
        (
            "previous_refresh_hash",
            Category.CONNECTION,
            "A fingerprint of the key it replaced, to notice if that one is used again",
        ),
        (
            "refresh_expires_at",
            Category.CONNECTION,
            "When you have to sign in again if you do not open the app before",
        ),
    ),
    # A renewal of Spotify access in progress (WHO-61): which account it renews,
    # so a different Spotify account is refused. Gone at claim or expiry.
    *_fields(
        "spotify_renewal",
        ("login_id", Category.CONNECTION, "Which sign-in is renewing your Spotify access"),
        ("user_id", Category.CONNECTION, "Which account the renewal is for"),
    ),
    # W2 "Gaps in collection": when collection stopped and started again. The
    # source, start and end only, kept as long as plays are (CTO, WHO-18 Q1).
    *_fields(
        "collection_gap",
        ("id", Category.COLLECTION_GAPS, "Our identifier for each gap in collection"),
        ("user_id", Category.COLLECTION_GAPS, "Which account the gap belongs to"),
        ("source", Category.COLLECTION_GAPS, "Which source we could not collect from"),
        ("started_at", Category.COLLECTION_GAPS, "When collection stopped"),
        (
            "ended_at",
            Category.COLLECTION_GAPS,
            "When collection started again, empty while it is still stopped",
        ),
    ),
)

# The only Spotify profile fields stored, as (profile key, column).
SPOTIFY_PROFILE_COLUMNS: Final[dict[str, str]] = {
    "id": "spotify_account_id",
    "display_name": "spotify_display_name",
}

# Columns that exist but never hold data about the user. Each needs a reason.
# Not shown on the screen; still checked, so a column cannot hide here unnoticed.
NOT_USER_DATA: Final[dict[tuple[str, str], str]] = {
    ("unified_event", "source_event_id"): (
        "NULL for every v1 source (Spotify, YouTube, Netflix have no event id). "
        "Move it to STORED_FIELDS in the change that first writes it."
    ),
}

# Tables that hold no data linked to any user. Everything else must be user-scoped
# and is covered by delete (see app.privacy.audit).
#
# oauth_login: one row per sign-in in progress, before any account exists. It
# has no user_id and no profile data: hashes of one-time secrets, the encrypted
# PKCE verifier, the device's time zone for a signup, ``is_renewal`` and
# ``import_recent_history`` (true for every sign-in since WHO-114). A
# renewal is tied to its user in ``spotify_renewal``, never here (WHO-61). A row is deleted when
# the app claims its session, or by the poller within OAUTH_LOGIN_TTL_SECONDS
# (10 minutes) of its creation, whichever is first.
SYSTEM_TABLES: Final[frozenset[str]] = frozenset({"alembic_version", "oauth_login"})
