"""Synthetic Spotify "Extended streaming history" exports for the import tests.

Rows have every field a real export row has, with synthetic values. The fields
we must never store carry ``SENTINELS``, so a test can search the whole
database for them. Nothing here is real user data.
"""

import json
import zipfile
from pathlib import Path
from typing import Any

# One distinctive value per field that must never reach the database or a log.
SENTINELS = {
    "ip_addr": "198.51.100.231",
    "conn_country": "sentinel-country",
    "platform": "sentinel-platform-os",
    "user_agent_decrypted": "sentinel-user-agent/9",
    "username": "sentinel-username",
    "reason_start": "sentinel-reason-start",
    "reason_end": "sentinel-reason-end",
    "offline_timestamp": 1234567891,
}

AUDIO_ENTRY = "Spotify Extended Streaming History/Streaming_History_Audio_2023-2024_0.json"
SECOND_AUDIO_ENTRY = "Spotify Extended Streaming History/Streaming_History_Audio_2024_1.json"


def track_id(n: int) -> str:
    """A 22-character base62 id, like Spotify's."""
    return f"SynthTrack{n:012d}"


def row(
    ts: str,
    n: int = 1,
    *,
    ms_played: int = 200_000,
    title: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One music row, played at ``ts`` (end of play), track ``n``."""
    value: dict[str, Any] = {
        "ts": ts,
        "username": SENTINELS["username"],
        "platform": SENTINELS["platform"],
        "ms_played": ms_played,
        "conn_country": SENTINELS["conn_country"],
        "ip_addr": SENTINELS["ip_addr"],
        "user_agent_decrypted": SENTINELS["user_agent_decrypted"],
        "master_metadata_track_name": title or f"Synthetic Song {n}",
        "master_metadata_album_artist_name": "Nova Kett",
        "master_metadata_album_album_name": "Synthetic Album",
        "spotify_track_uri": f"spotify:track:{track_id(n)}",
        "episode_name": None,
        "episode_show_name": None,
        "spotify_episode_uri": None,
        "audiobook_title": None,
        "audiobook_uri": None,
        "reason_start": SENTINELS["reason_start"],
        "reason_end": SENTINELS["reason_end"],
        "shuffle": True,
        "skipped": False,
        "offline": True,
        "offline_timestamp": SENTINELS["offline_timestamp"],
        "incognito_mode": False,
    }
    value.update(overrides)
    return value


def podcast_row(ts: str) -> dict[str, Any]:
    return row(
        ts,
        master_metadata_track_name=None,
        master_metadata_album_artist_name=None,
        master_metadata_album_album_name=None,
        spotify_track_uri=None,
        episode_name="Synthetic Episode",
        episode_show_name="Synthetic Show",
        spotify_episode_uri="spotify:episode:SynthEpisode0000000001",
    )


def json_bytes(rows: list[dict[str, Any]]) -> bytes:
    # Spotify writes indented JSON; so do we, to cross read boundaries often.
    return json.dumps(rows, indent=2, ensure_ascii=False).encode()


def write_zip(
    path: Path,
    entries: dict[str, bytes],
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def export_zip(path: Path, *audio: list[dict[str, Any]]) -> Path:
    """A zip shaped like ``my_spotify_data.zip``, with other files beside the audio."""
    entries = {
        "Spotify Extended Streaming History/ReadMeFirst_ExtendedStreamingHistory.pdf": (
            b"%PDF-1.4 synthetic"
        ),
        "Spotify Extended Streaming History/Streaming_History_Video_2023.json": json_bytes(
            [row("2023-01-01T00:00:00Z", 999)]
        ),
        "Spotify Account Data/Userdata.json": json.dumps(
            {"username": SENTINELS["username"]}
        ).encode(),
    }
    names = [AUDIO_ENTRY, SECOND_AUDIO_ENTRY]
    for name, rows in zip(names, audio, strict=False):
        entries[name] = json_bytes(rows)
    return write_zip(path, entries)
