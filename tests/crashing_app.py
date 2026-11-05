"""The real app plus one test-only route that crashes with a database error.

Served by uvicorn in ``test_privacy_logs``. The error text carries
``CRASH_MARKER`` the way Postgres puts a stored row in DETAIL, so a substring
check on the server output shows whether that text was logged.
"""

import psycopg
from sqlalchemy.exc import IntegrityError

from app.api.main import app

CRASH_PATH = "/test-only/crash"
CRASH_MARKER = "Synthetic Crash Title 0b7e"


@app.get(CRASH_PATH)
def crash() -> None:
    orig = psycopg.errors.NotNullViolation(
        'null value in column "played_at" violates not-null constraint\n'
        f"DETAIL:  Failing row contains ({CRASH_MARKER}, null)."
    )
    raise IntegrityError("INSERT INTO play_probe VALUES (%s, NULL)", None, orig) from orig
