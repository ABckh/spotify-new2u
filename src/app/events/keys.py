"""The dedupe key recipe, schema revision 3 §4.1. Frozen.

::

    item_key   = "id:" || provider_item_id                    -- when the source has one
               | "nt:" || left(sha256(normalize(title)), 32)  -- when it does not
    normalize(t) = regexp_replace(trim(casefold(NFKC(t))), '\\s+', ' ', 'g')
    local_date = (occurred_at AT TIME ZONE home_timezone)::date   -- every row
    stamp      = occurred_at in UTC as YYYY-MM-DDTHH:MM:SSZ        -- precision 'second'
               | local_date as YYYY-MM-DD                          -- precision 'day'
    dedupe_key = sha256(version|user_id|source|precision|stamp|item_key)
    day_key    = sha256(version|user_id|source|local_date|item_key)

Any change to this module is a ``dedupe_key_version`` bump with an in-place
recompute and a new schema sign-off, never a silent edit: a changed byte
re-keys every row and the next poll stores everything twice.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

DEDUPE_KEY_VERSION = 1

Precision = Literal["second", "day"]
ItemKeySource = Literal["provider_id", "normalized_title"]

_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", title).casefold().strip())


@dataclass(frozen=True)
class ItemKey:
    value: str
    source: ItemKeySource


def item_key_from_provider_id(provider_item_id: str) -> ItemKey:
    if not provider_item_id:
        raise ValueError("provider item id is empty")
    return ItemKey(f"id:{provider_item_id}", "provider_id")


def item_key_from_title(title: str) -> ItemKey:
    digest = hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()
    return ItemKey(f"nt:{digest[:32]}", "normalized_title")


def _require_aware(occurred_at: datetime) -> None:
    # A naive datetime is rejected, never assumed to be UTC (§9 timezone guard).
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")


def local_date(occurred_at: datetime, home_timezone: str) -> date:
    _require_aware(occurred_at)
    return occurred_at.astimezone(ZoneInfo(home_timezone)).date()


def stamp(occurred_at: datetime, precision: Precision, home_timezone: str) -> str:
    _require_aware(occurred_at)
    if precision == "second":
        # strftime drops the fraction: truncation to the second, as to_char does.
        return occurred_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return local_date(occurred_at, home_timezone).isoformat()


def _sha256(*parts: object) -> bytes:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()


def dedupe_key(
    *,
    user_id: UUID,
    source: str,
    precision: Precision,
    occurred_at: datetime,
    item_key: str,
    home_timezone: str,
    version: int = DEDUPE_KEY_VERSION,
) -> bytes:
    return _sha256(
        version, user_id, source, precision, stamp(occurred_at, precision, home_timezone), item_key
    )


def day_key(
    *,
    user_id: UUID,
    source: str,
    occurred_at: datetime,
    item_key: str,
    home_timezone: str,
    version: int = DEDUPE_KEY_VERSION,
) -> bytes:
    return _sha256(
        version, user_id, source, local_date(occurred_at, home_timezone).isoformat(), item_key
    )
