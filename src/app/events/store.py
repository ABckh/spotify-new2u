"""Writing unified events: the precise-row statement of schema revision 3 §5.3.

Every writer of ``unified_event`` goes through here. A row is inserted, or
upgrades a day-grain row with the same ``day_key`` (§4.4), or is a no-op
because its ``dedupe_key`` is already stored. Re-writing a stored play
therefore adds no row, which is what makes a re-poll of an overlapping window
add zero rows.

One exception to "changes nothing" (WHO-135): a ``provider_extras`` key the
stored row lacks is added to it (``filled``). Keys it already has are never
overwritten. This is how a later poll or history import repairs rows stored
before a key was collected. ``artwork_url`` is treated the same way (WHO-149):
set when the stored row has none, never replaced.

Only precise (``second``) rows have a writer today: Spotify emits nothing else.
The coarse-row statement lands with the Netflix import.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from app.events import keys
from app.privacy.provider_extras import check_provider_extras

Kind = Literal["listen", "watch"]
# api_backfill: the recent plays a provider already lists, imported once at the
# user's request when they first connect (WHO-89).
IngestMethod = Literal["api_poll", "api_backfill", "file_import"]


@dataclass(frozen=True)
class PreciseEvent:
    """One play known to the second. ``occurred_at`` must be timezone-aware."""

    source: str
    source_artifact: str
    ingest_method: IngestMethod
    kind: Kind
    occurred_at: datetime
    title: str
    item_key: keys.ItemKey
    duration_ms: int | None = None
    # Never inferred from duration_ms. NULL unless the provider measured it.
    played_ms: int | None = None
    creators: tuple[str, ...] = ()
    collection: str | None = None
    # Checked by the caller (``app.spotify.artwork.safe_artwork_url`` for Spotify).
    artwork_url: str | None = None
    external_ids: dict[str, str] = field(default_factory=dict)
    provider_extras: dict[str, object] = field(default_factory=dict)


@dataclass
class WriteCounts:
    inserted: int = 0
    upgraded: int = 0
    skipped: int = 0
    # Already stored, and given a provider_extras key or an artwork URL it lacked.
    # Also counted in skipped.
    filled: int = 0


def lock_user_source(conn: Connection, user_id: UUID, source: str) -> None:
    """Serialize writers per (user, source) for this transaction (§4.4)."""
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:user_id), hashtext(:source))"),
        {"user_id": str(user_id), "source": source},
    )


_WRITE_PRECISE = text(
    """
    WITH upgraded AS (
        UPDATE unified_event SET
            occurred_at           = :occurred_at,
            occurred_at_precision = 'second',
            dedupe_key            = :dedupe_key,
            source_artifact       = :source_artifact,
            title                 = :title,
            duration_ms           = COALESCE(:duration_ms, unified_event.duration_ms),
            played_ms             = COALESCE(:played_ms, unified_event.played_ms),
            collection            = COALESCE(:collection, unified_event.collection),
            artwork_url           = COALESCE(CAST(:artwork_url AS text), unified_event.artwork_url),
            creators              = CASE WHEN cardinality(CAST(:creators AS text[])) > 0
                                         THEN CAST(:creators AS text[])
                                         ELSE unified_event.creators END,
            external_ids          = unified_event.external_ids || CAST(:external_ids AS jsonb),
            provider_extras       = CAST(:provider_extras AS jsonb) || unified_event.provider_extras
        -- id, ingest_batch_id and ingested_at are deliberately not set:
        -- the row stays on the batch that inserted it (index 5, §5.2).
        WHERE user_id = :user_id
          AND day_key = :day_key
          AND occurred_at_precision = 'day'
        RETURNING id
    ),
    -- The same play already stored: add only the extras keys it lacks, and the
    -- artwork URL if it has none. ``a || b`` keeps b's value for a shared key, so
    -- a stored value always wins. A day row has another dedupe_key, so this
    -- never touches the row ``upgraded`` changes.
    filled AS (
        UPDATE unified_event SET
            provider_extras = CAST(:provider_extras AS jsonb) || unified_event.provider_extras,
            artwork_url     = COALESCE(unified_event.artwork_url, CAST(:artwork_url AS text))
        WHERE user_id = :user_id
          AND dedupe_key = :dedupe_key
          AND (CAST(:provider_extras AS jsonb) || unified_event.provider_extras
                   <> unified_event.provider_extras
               OR (unified_event.artwork_url IS NULL AND CAST(:artwork_url AS text) IS NOT NULL))
        RETURNING id
    ),
    inserted AS (
        INSERT INTO unified_event (
            user_id, source, source_artifact, ingest_method, ingest_batch_id,
            kind, occurred_at, occurred_at_precision, duration_ms, played_ms,
            title, creators, collection, artwork_url, external_ids, provider_extras,
            item_key, item_key_source, dedupe_key, day_key, dedupe_key_version
        )
        SELECT
            :user_id, :source, :source_artifact, :ingest_method, :ingest_batch_id,
            :kind, :occurred_at, 'second', :duration_ms, :played_ms,
            :title, CAST(:creators AS text[]), :collection, CAST(:artwork_url AS text),
            CAST(:external_ids AS jsonb), CAST(:provider_extras AS jsonb),
            :item_key, :item_key_source, :dedupe_key, :day_key, :version
        WHERE NOT EXISTS (SELECT 1 FROM upgraded)
        ON CONFLICT (user_id, dedupe_key) DO NOTHING
        RETURNING id
    )
    SELECT (SELECT count(*) FROM upgraded) AS upgraded,
           (SELECT count(*) FROM inserted) AS inserted,
           (SELECT count(*) FROM filled) AS filled
    """
)


def write_precise_event(
    conn: Connection,
    *,
    user_id: UUID,
    home_timezone: str,
    event: PreciseEvent,
    ingest_batch_id: UUID | None,
    counts: WriteCounts,
) -> None:
    """Insert, upgrade or skip one precise row. Caller holds ``lock_user_source``."""
    check_provider_extras(event.source, event.provider_extras)
    params = {
        "user_id": user_id,
        "source": event.source,
        "source_artifact": event.source_artifact,
        "ingest_method": event.ingest_method,
        "ingest_batch_id": ingest_batch_id,
        "kind": event.kind,
        "occurred_at": event.occurred_at,
        "duration_ms": event.duration_ms,
        "played_ms": event.played_ms,
        "title": event.title,
        "creators": list(event.creators),
        "collection": event.collection,
        "artwork_url": event.artwork_url,
        "external_ids": json.dumps(event.external_ids),
        "provider_extras": json.dumps(event.provider_extras),
        "item_key": event.item_key.value,
        "item_key_source": event.item_key.source,
        "dedupe_key": keys.dedupe_key(
            user_id=user_id,
            source=event.source,
            precision="second",
            occurred_at=event.occurred_at,
            item_key=event.item_key.value,
            home_timezone=home_timezone,
        ),
        "day_key": keys.day_key(
            user_id=user_id,
            source=event.source,
            occurred_at=event.occurred_at,
            item_key=event.item_key.value,
            home_timezone=home_timezone,
        ),
        "version": keys.DEDUPE_KEY_VERSION,
    }
    row = conn.execute(_WRITE_PRECISE, params).one()
    counts.upgraded += row.upgraded
    counts.inserted += row.inserted
    counts.skipped += 1 - row.upgraded - row.inserted
    counts.filled += row.filled
