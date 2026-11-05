"""QA (WHO-107): independent acceptance checks for the history import, synthetic data only.

Written by QA, not by the import's author. Drives the real HTTP routes, the real
worker and the real poller (against the fake Spotify) on a real Postgres.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.poller import spotify as poller_spotify
from fake_spotify import FakeSpotify
from history_export import export_zip, podcast_row, row, track_id
from test_history_import import (
    NOW,
    T0,
    Account,
    accepted,
    account,
    api_play,
    client,
    events,
    imports_dir,
    latest,
    run_worker,
    scalar,
)

__all__ = ["account", "client", "imports_dir"]  # fixtures

NEAR_DUPLICATES = (
    "SELECT count(*) FROM unified_event a JOIN unified_event b "
    "  ON a.user_id = b.user_id AND a.item_key = b.item_key AND a.id < b.id "
    " AND abs(extract(epoch FROM a.occurred_at - b.occurred_at)) <= 10 "
    "WHERE a.user_id = :u"
)
KEY_COLLISIONS = (
    "SELECT count(*) FROM (SELECT dedupe_key FROM unified_event WHERE user_id = :u "
    "GROUP BY dedupe_key HAVING count(*) > 1) d"
)


def z(at: Any) -> str:
    return str(at.isoformat().replace("+00:00", "Z"))


def poll(engine: Engine, fake: FakeSpotify, account: Account, at: Any) -> Any:
    return poller_spotify.poll_connection(engine, fake.client(), account.id, at)


def test_qa1_poll_after_import_does_not_duplicate_an_offset_play(
    client: TestClient, engine: Engine, account: Account, fake_spotify: FakeSpotify, tmp_path: Path
) -> None:
    """The poller was down; the export holds plays it never saw; then it comes back."""
    api_play(engine, account, T0 + timedelta(hours=1), 1)  # last poll before the outage
    play_end = T0 + timedelta(hours=2)
    accepted(client, account, export_zip(tmp_path / "e.zip", [row(z(play_end), 7)]))
    assert run_worker(engine) == "succeeded"
    # recently-played reports the same play 3 s before the export's ts (inside the 10 s window).
    fake_spotify.play(track_id(7), "Synthetic Song 7", play_end - timedelta(seconds=3))

    assert poll(engine, fake_spotify, account, NOW).status == "succeeded"

    assert scalar(engine, NEAR_DUPLICATES, u=account.id) == 0
    assert len(events(engine, account.id)) == 2


def test_qa2_poll_after_import_same_second_is_not_duplicated(
    client: TestClient, engine: Engine, account: Account, fake_spotify: FakeSpotify, tmp_path: Path
) -> None:
    api_play(engine, account, T0 + timedelta(hours=1), 1)
    play_end = T0 + timedelta(hours=2)
    accepted(client, account, export_zip(tmp_path / "e.zip", [row(z(play_end), 7)]))
    assert run_worker(engine) == "succeeded"
    fake_spotify.play(track_id(7), "Synthetic Song 7", play_end + timedelta(milliseconds=400))

    assert poll(engine, fake_spotify, account, NOW).status == "succeeded"

    assert scalar(engine, KEY_COLLISIONS, u=account.id) == 0
    assert len(events(engine, account.id)) == 2


def test_qa3_refresh_after_import_fetches_only_new_plays(
    client: TestClient, engine: Engine, account: Account, fake_spotify: FakeSpotify, tmp_path: Path
) -> None:
    polled = T0 + timedelta(hours=1)
    fake_spotify.play(track_id(1), "Synthetic Song 1", polled)
    assert poll(engine, fake_spotify, account, polled + timedelta(minutes=5)).rows_inserted == 1
    old = [row(f"2023-0{m}-01T10:00:00Z", 100 + m) for m in range(1, 10)]
    accepted(client, account, export_zip(tmp_path / "e.zip", old))
    assert run_worker(engine) == "succeeded"
    fake_spotify.play(track_id(2), "Synthetic Song 2", NOW - timedelta(hours=1))

    result = poll(engine, fake_spotify, account, NOW)

    assert (result.status, result.rows_inserted) == ("succeeded", 1)
    after = int(fake_spotify.calls("/v1/me/player/recently-played")[-1].url.params["after"])
    assert after == int(polled.timestamp() * 1000)  # the cursor ignores the import
    assert len(events(engine, account.id)) == 11
    assert scalar(engine, NEAR_DUPLICATES, u=account.id) == 0


def test_qa4_timeline_shows_only_music_of_30_s_or_more(
    client: TestClient, engine: Engine, account: Account, tmp_path: Path
) -> None:
    rows = [
        row("2024-01-01T10:00:00Z", 1, title="QA keep plain"),
        row("2024-01-01T10:05:00Z", 2, ms_played=30_000, title="QA keep exactly 30 s"),
        row("2024-01-01T10:06:00Z", 3, ms_played=29_999, title="QA drop 29.999 s"),
        row("2024-01-01T10:07:00Z", 4, incognito_mode=True, title="QA drop private"),
        podcast_row("2024-01-01T10:08:00Z"),
        row(
            "2024-01-01T10:09:00Z",
            5,
            title="QA drop audiobook",
            master_metadata_track_name=None,
            spotify_track_uri=None,
            audiobook_title="Synthetic Audiobook",
            audiobook_uri="spotify:audiobook:SynthAudiobook00000001",
        ),
    ]
    accepted(client, account, export_zip(tmp_path / "e.zip", rows))
    assert run_worker(engine) == "succeeded"

    body = client.get(
        "/v1/timeline", params={"period": "day", "anchor": "2024-01-01"}, headers=account.auth
    ).json()

    titles = sorted(e["title"] for e in body["events"])
    assert titles == ["QA keep exactly 30 s", "QA keep plain"]  # no video row 999 either
    counts = latest(client, account)["counts"]
    assert counts["rows_inserted"] == 2


def test_qa5_overlapping_import_after_failure_and_polls_leave_no_duplicates(
    client: TestClient, engine: Engine, account: Account, fake_spotify: FakeSpotify, tmp_path: Path
) -> None:
    """Fail once (bad JSON), retry with a superset, poll around it: one row per play."""
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'[{"ts": "2024-01-01T10:00:00Z", ')
    accepted(client, account, bad)
    assert run_worker(engine) == "failed"
    assert latest(client, account)["status"] == "failed"
    assert len(events(engine, account.id)) == 0

    at = T0 + timedelta(minutes=30)
    fake_spotify.play(track_id(50), "Synthetic Song 50", at)
    poll(engine, fake_spotify, account, at + timedelta(minutes=1))
    plays = [row(f"2024-03-{d:02d}T10:00:00Z", d) for d in range(1, 29)]
    plays.append(row(z(at + timedelta(seconds=2)), 50))  # the polled play, 2 s off
    accepted(client, account, export_zip(tmp_path / "e.zip", plays, plays[:10]))
    assert run_worker(engine) == "succeeded"

    assert len(events(engine, account.id)) == 29
    assert scalar(engine, NEAR_DUPLICATES, u=account.id) == 0
    assert scalar(engine, KEY_COLLISIONS, u=account.id) == 0
    second = client.post(
        "/v1/spotify/history-import",
        headers=account.auth,
        files={"file": ("e.zip", (tmp_path / "e.zip").read_bytes(), "application/zip")},
    )
    assert (second.status_code, second.json()["detail"]["code"]) == (
        409,
        "history_import_already_done",
    )
