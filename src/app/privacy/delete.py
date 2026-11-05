"""The one delete-everything path (PRD P3/P5, story S8).

Deleting the ``app_user`` row cascades to every user table, including stored
provider tokens. ``app.privacy.audit.delete_coverage_problems`` is what keeps
that true when a table is added. A ``provider_extras`` key, such as
``spotify_album_total_tracks`` (WHO-141) or ``spotify_album_type`` (WHO-162),
goes with its ``unified_event`` row.

Every account delete writes exactly one log line, ``account_deleted user_id=<id>``
(WHO-26 decision, section 4). A restore from backup brings deleted accounts back;
the "Restore" runbook in docs/self-hosting.md replays these lines to delete them again.

Run as a CLI for that replay: ``python -m app.privacy.delete <user_id> [...]``.
"""

import argparse
import logging
import sys
from uuid import UUID

from sqlalchemy import Connection, text

from app.core.db import get_engine
from app.core.log import configure_logging
from app.spotify.history_import import upload_path

logger = logging.getLogger("app.privacy.delete")
# The delete line is what the restore replay depends on, so it must not be
# dropped when LOG_LEVEL is raised to WARNING for the rest of the app.
logger.setLevel(logging.INFO)


def delete_all_user_data(conn: Connection, user_id: UUID, schema: str = "public") -> int:
    """Delete everything held about ``user_id``. The caller owns the transaction.

    Returns the number of ``app_user`` rows deleted: 1 if the account existed,
    0 if not. ``DELETE /v1/me`` checks it is exactly 1 (WHO-25 R4).

    The log line is written before the caller commits. If the transaction then
    rolls back, the journal holds a line for an account that still exists, and a
    later restore replay deletes it: that is an account whose owner asked for
    deletion, so the error is in the safe direction. Logging after commit would
    risk the opposite: a deleted account with no line, silently restored.
    """
    pending_uploads = _pending_history_uploads(conn, user_id, schema)
    result = conn.execute(
        text(f'DELETE FROM "{schema}"."app_user" WHERE id = :user_id'), {"user_id": user_id}
    )
    deleted: int = result.rowcount
    if deleted == 0:
        return 0
    # An uploaded history export waiting for its import (WHO-104) is the user's
    # full listening history: gone now, not at the 24-hour sweep. If the
    # transaction rolls back, that import fails with upload_missing instead.
    for import_id in pending_uploads:
        upload_path(import_id).unlink(missing_ok=True)
    # The id only. No email, no provider ids, no counts: this line outlives the data.
    logger.info("account_deleted user_id=%s", user_id)
    return deleted


def _pending_history_uploads(conn: Connection, user_id: UUID, schema: str) -> list[UUID]:
    """Ids of the user's history imports that may still have an upload file."""
    exists = conn.execute(
        text("SELECT to_regclass(:name)"), {"name": f'"{schema}"."history_import"'}
    ).scalar_one()
    if exists is None:
        return []
    return list(
        conn.execute(
            text(
                f'SELECT id FROM "{schema}"."history_import" '
                "WHERE user_id = :user_id AND status IN ('queued', 'running')"
            ),
            {"user_id": user_id},
        ).scalars()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.privacy.delete",
        description="Delete accounts and all their data. Used by the restore replay.",
    )
    parser.add_argument("user_ids", nargs="+", type=UUID, metavar="user_id")
    args = parser.parse_args(argv)

    configure_logging()
    # One transaction per id, so one failure does not undo the deletes before it.
    for user_id in args.user_ids:
        with get_engine().begin() as conn:
            if not delete_all_user_data(conn, user_id):
                print(f"not present: user_id={user_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
