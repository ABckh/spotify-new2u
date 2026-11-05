"""PRD P12: no log holds the callback's query string or anything the user played.

The synthetic values below are distinctive so a plain substring check proves
their absence.
"""

import io
import logging
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text

from app.core.log import WITHHELD, PrivacyFormatter

CALLBACK = "/auth/spotify/callback"
SYNTHETIC_CODE = "synthetic-oauth-code-7f3a"
SYNTHETIC_STATE = "synthetic-oauth-state-91c2"
SYNTHETIC_TITLE = "Synthetic Track Title 4e1d"
CALLBACK_URL = f"{CALLBACK}?code={SYNTHETIC_CODE}&state={SYNTHETIC_STATE}"
# Served by crashing_app.py, which uvicorn imports in its own process only.
CRASH_PATH = "/test-only/crash"
CRASH_MARKER = "Synthetic Crash Title 0b7e"


def test_access_log_records_route_and_client_but_never_the_query_string(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    client.get(CALLBACK_URL)

    lines = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert len(lines) == 1
    assert f"route={CALLBACK} " in lines[0]
    assert "user_id=- client=testclient" in lines[0]
    assert_no_secret(caplog.text)


def test_uvicorn_process_never_logs_the_callback_query_string() -> None:
    """The real server, started without ``--no-access-log``: the code turns it off."""
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api.main:app", "--port", str(port)],
        env={**os.environ, "PYTHONPATH": "src", "LOG_LEVEL": "DEBUG"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_until_up(base)
        httpx.get(base + CALLBACK_URL)
    finally:
        server.terminate()
        output, _ = server.communicate(timeout=10)

    assert f"request method=GET route={CALLBACK} status=400 user_id=- client=127.0.0.1" in output
    assert_no_secret(output)


def test_uvicorn_process_withholds_database_error_text_from_a_route_crash() -> None:
    """Uvicorn logs a route's crash on its own logger, not through ``app.*``."""
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "crashing_app:app", "--port", str(port)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(["src", "tests"]), "LOG_LEVEL": "DEBUG"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_until_up(base)
        response = httpx.get(base + CRASH_PATH)
    finally:
        server.terminate()
        output, _ = server.communicate(timeout=10)

    assert response.status_code == 500
    assert "Exception in ASGI application" in output
    assert "sqlalchemy.exc.IntegrityError" in output
    assert "sqlstate=23502" in output
    assert WITHHELD in output
    assert CRASH_MARKER not in output


def test_database_error_text_is_withheld_from_a_logged_traceback(db: Connection) -> None:
    db.execute(text("CREATE TEMP TABLE play_probe (title text, played_at timestamptz NOT NULL)"))
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(PrivacyFormatter("%(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("tests.privacy_logs")
    logger.addHandler(handler)
    try:
        # Postgres answers with DETAIL "Failing row contains (<title>, null)".
        db.execute(text("INSERT INTO play_probe VALUES (:title, NULL)"), {"title": SYNTHETIC_TITLE})
    except Exception:
        logger.exception("poll tick failed")
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert "poll tick failed" in output
    assert "sqlalchemy.exc.IntegrityError" in output
    assert "psycopg.errors.NotNullViolation" in output
    assert "sqlstate=23502" in output
    assert WITHHELD in output
    assert SYNTHETIC_TITLE not in output


def test_other_errors_keep_their_message_and_chain() -> None:
    formatter = PrivacyFormatter()
    try:
        try:
            raise KeyError("inner")
        except KeyError as exc:
            raise RuntimeError("outer") from exc
    except RuntimeError:
        output = formatter.formatException(sys.exc_info())

    assert "KeyError: 'inner'" in output
    assert "direct cause of the following exception" in output
    assert output.endswith("RuntimeError: outer")


def assert_no_secret(output: str) -> None:
    for value in (SYNTHETIC_CODE, SYNTHETIC_STATE, "code=", "state="):
        assert value not in output


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _wait_until_up(base: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            httpx.get(base + "/healthz")
            return
        except httpx.TransportError:
            time.sleep(0.1)
    pytest.fail("uvicorn did not start")
