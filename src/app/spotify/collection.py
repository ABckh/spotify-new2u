"""Polls store only plays made while connected (PRD P15, S1.8a, S8.9).

``spotify_connection.collection_started_at`` is set to the moment the user
completes a connect, on every connect: the first one, a reconnect after a
disconnect, and a reconnect after a delete (which is a first connect again).
Spotify's recently-played list returns up to 50 plays and some predate that
moment; ``plays_made_while_connected`` drops them from every ``api_poll``
before anything is written.

The one exception is the history import (WHO-89): after a first connect the
poller stores the 50 plays Spotify lists once, unfiltered, as
``ingest_method = 'api_backfill'`` (``app.poller.spotify.import_recent_history``).
It is on for every first connect (WHO-114), it stores only what Spotify
returns, and ``collection_started_at`` still marks where live
collection begins. No collection gap covers the time before it.

Whether a reconnect after Spotify revoked our access (or the 6-month expiry)
resets the moment is open question O7 on WHO-20; the connect code decides that
when O7 is answered, not this function.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, TypeVar


class Play(Protocol):
    @property
    def played_at(self) -> datetime: ...


P = TypeVar("P", bound=Play)


def plays_made_while_connected(plays: Iterable[P], collection_started_at: datetime) -> list[P]:
    """Keep the plays whose ``played_at`` is at or after ``collection_started_at``."""
    if collection_started_at.tzinfo is None:
        raise ValueError("collection_started_at must be timezone-aware")
    kept = []
    for play in plays:
        if play.played_at.tzinfo is None:
            raise ValueError("played_at must be timezone-aware")
        if play.played_at >= collection_started_at:
            kept.append(play)
    return kept
