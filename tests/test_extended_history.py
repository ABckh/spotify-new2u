"""Reading the Spotify extended history export: limits, safe zip, row rules (WHO-104).

No database: these are the checks on the file itself. Every file is built in
``tmp_path`` from synthetic rows (``history_export``).
"""

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.spotify import extended_history as eh
from app.spotify.extended_history import ExportPlay, ImportRejected, Skip, play_from_row
from history_export import (
    AUDIO_ENTRY,
    SECOND_AUDIO_ENTRY,
    export_zip,
    json_bytes,
    podcast_row,
    row,
    track_id,
    write_zip,
)


def rejected(code: str) -> pytest.RaisesExc[ImportRejected]:
    return pytest.raises(ImportRejected, match=f"^{code}$")


def all_rows(path: Path) -> list[object]:
    return list(eh.iter_rows(path, eh.inspect_upload(path)))


# --- what is read -----------------------------------------------------------


def test_a_zip_is_read_from_its_audio_entries_only(tmp_path: Path) -> None:
    first = [row("2024-01-01T10:00:00Z", 1), row("2024-01-01T10:05:00Z", 2)]
    second = [row("2024-02-01T10:00:00Z", 3)]
    path = export_zip(tmp_path / "my_spotify_data.zip", first, second)

    assert eh.inspect_upload(path) == "zip"
    # Userdata.json and the video history are never opened: track 999 lives there.
    assert all_rows(path) == first + second


def test_a_single_audio_json_is_read(tmp_path: Path) -> None:
    rows = [row("2024-01-01T10:00:00Z", 1)]
    path = tmp_path / "Streaming_History_Audio_2024.json"
    path.write_bytes(json_bytes(rows))

    assert eh.inspect_upload(path) == "json"
    assert all_rows(path) == rows


def test_rows_split_across_reads_decode_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seven-byte reads split numbers, strings and multi-byte characters.
    monkeypatch.setattr(eh, "_READ_CHUNK", 7)
    rows = [
        row("2024-01-01T10:00:00Z", n, title="Zoë – 🎵 Ünïcode", ms_played=123456) for n in range(5)
    ]
    path = tmp_path / "Streaming_History_Audio_2024.json"
    path.write_bytes(b"\xef\xbb\xbf" + json_bytes(rows))

    assert all_rows(path) == rows


def test_an_empty_array_has_no_rows(tmp_path: Path) -> None:
    path = tmp_path / "Streaming_History_Audio_2024.json"
    path.write_bytes(b"[ ]\n")

    assert all_rows(path) == []


@pytest.mark.parametrize(
    "name",
    [
        "Streaming_History_Audio_2024.json",
        "my_spotify_data/Spotify Extended Streaming History/Streaming_History_Audio_2024_3.json",
    ],
)
def test_audio_entries_are_found_in_any_folder(name: str) -> None:
    assert eh.is_audio_entry(name)


@pytest.mark.parametrize(
    "name",
    [
        "Streaming_History_Video_2024.json",
        "Userdata.json",
        "__MACOSX/._Streaming_History_Audio_2024.json",
        "Streaming_History_Audio_2024.json.txt",
        "StreamingHistory_music_0.json",
    ],
)
def test_other_entries_are_not_read(name: str) -> None:
    assert not eh.is_audio_entry(name)


# --- what is rejected -------------------------------------------------------


def test_a_zip_bomb_entry_is_rejected(tmp_path: Path) -> None:
    # 20 MB of zeros deflate to about 20 KB: about 1000:1.
    path = write_zip(tmp_path / "bomb.zip", {AUDIO_ENTRY: b"[" + b" " * (20 * 1024 * 1024) + b"]"})

    with rejected("zip_ratio_exceeded"):
        eh.inspect_upload(path)


def test_a_zip_over_the_uncompressed_total_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eh, "MAX_ZIP_UNCOMPRESSED_BYTES", 1000)
    data = json_bytes([row("2024-01-01T10:00:00Z", n) for n in range(5)])
    path = write_zip(tmp_path / "big.zip", {AUDIO_ENTRY: data}, zipfile.ZIP_STORED)

    with rejected("zip_too_large"):
        eh.inspect_upload(path)


def test_an_entry_that_inflates_past_its_limit_is_stopped_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared size can lie; the job counts what it decompresses."""
    path = export_zip(tmp_path / "e.zip", [row("2024-01-01T10:00:00Z", n) for n in range(50)])
    assert eh.inspect_upload(path) == "zip"
    monkeypatch.setattr(eh, "MAX_ENTRY_BYTES", 2000)
    monkeypatch.setattr(eh, "_audio_entries", _entries_without_size_check)

    with rejected("entry_too_large"):
        list(eh.iter_rows(path, "zip"))


def _entries_without_size_check(path: Path) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(path) as zf:
        return [i for i in zf.infolist() if eh.is_audio_entry(i.filename)]


def test_an_audio_entry_over_50_mb_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eh, "MAX_ENTRY_BYTES", 100)
    path = export_zip(tmp_path / "e.zip", [row("2024-01-01T10:00:00Z")])

    with rejected("entry_too_large"):
        eh.inspect_upload(path)


@pytest.mark.parametrize(
    "name",
    [
        "../Streaming_History_Audio_2024.json",
        "a/../../Streaming_History_Audio_2024.json",
        "/etc/Streaming_History_Audio_2024.json",
        "C:/Streaming_History_Audio_2024.json",
        "a\\..\\Streaming_History_Audio_2024.json",
    ],
)
def test_path_traversal_is_rejected(tmp_path: Path, name: str) -> None:
    path = write_zip(
        tmp_path / "t.zip",
        {AUDIO_ENTRY: json_bytes([row("2024-01-01T10:00:00Z")]), name: b"[]"},
    )

    with rejected("zip_unsafe_path"):
        eh.inspect_upload(path)


def test_a_nested_zip_is_rejected(tmp_path: Path) -> None:
    inner = write_zip(tmp_path / "inner.zip", {AUDIO_ENTRY: b"[]"}).read_bytes()
    path = write_zip(
        tmp_path / "outer.zip",
        {AUDIO_ENTRY: json_bytes([row("2024-01-01T10:00:00Z")]), "more/inner.zip": inner},
        zipfile.ZIP_STORED,
    )

    with rejected("zip_nested_archive"):
        eh.inspect_upload(path)


def test_more_than_100_entries_is_rejected(tmp_path: Path) -> None:
    entries = {f"other/{n}.txt": b"x" for n in range(100)}
    entries[AUDIO_ENTRY] = b"[]"
    path = write_zip(tmp_path / "many.zip", entries)

    with rejected("zip_too_many_entries"):
        eh.inspect_upload(path)


def test_a_zip_without_audio_history_is_rejected(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "other.zip", {"Spotify Account Data/Userdata.json": b"{}"})

    with rejected("no_streaming_history"):
        eh.inspect_upload(path)


@pytest.mark.parametrize(
    "content",
    [
        b"%PDF-1.4 not an export",
        b"hello",
        b"",
        b'{"ts": "2024-01-01T00:00:00Z"}',
        b"PK\x03\x04broken",
    ],
)
def test_a_file_that_is_not_an_export_is_rejected(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "upload"
    path.write_bytes(content)

    with rejected("unsupported_file"):
        eh.inspect_upload(path)


def test_an_upload_over_200_mb_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eh, "MAX_UPLOAD_BYTES", 100)
    path = export_zip(tmp_path / "e.zip", [row("2024-01-01T10:00:00Z")])

    with rejected("upload_too_large"):
        eh.inspect_upload(path)


@pytest.mark.parametrize(
    "content",
    [
        b"[1,]",
        b"[{}] trailing",
        b'[{"ts": "2024-01',
        b"[\xff\xfe]",
        b"[" + b'"' + b"x" * 70_000 + b'"]',
    ],
)
def test_broken_json_fails_the_job(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "Streaming_History_Audio_2024.json"
    path.write_bytes(content)

    with rejected("invalid_json"):
        list(eh.iter_rows(path, "json"))


def test_more_than_the_row_limit_fails_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eh, "MAX_ROWS", 2)
    path = export_zip(
        tmp_path / "e.zip",
        [row("2024-01-01T10:00:00Z", 1), row("2024-01-01T10:05:00Z", 2)],
        [row("2024-01-01T10:10:00Z", 3)],
    )

    with rejected("too_many_rows"):
        all_rows(path)


def test_zip_entries_are_read_in_name_order(tmp_path: Path) -> None:
    path = write_zip(
        tmp_path / "e.zip",
        {
            SECOND_AUDIO_ENTRY: json_bytes([row("2024-02-01T10:00:00Z", 2)]),
            AUDIO_ENTRY: json_bytes([row("2024-01-01T10:00:00Z", 1)]),
        },
    )

    assert [r["spotify_track_uri"] for r in all_rows(path)] == [  # type: ignore[index]
        f"spotify:track:{track_id(1)}",
        f"spotify:track:{track_id(2)}",
    ]


# --- row rules (decision 7) and mapping (decision 8) ------------------------


def test_a_music_row_maps_to_a_precise_event() -> None:
    mapped = play_from_row(row("2024-03-01T12:34:56Z", 7, ms_played=187_000))

    assert isinstance(mapped, ExportPlay)
    event = mapped.event
    assert event.source == "spotify"
    assert event.source_artifact == "spotify_extended_history"
    assert event.ingest_method == "file_import"
    assert event.kind == "listen"
    assert event.occurred_at == datetime(2024, 3, 1, 12, 34, 56, tzinfo=UTC)
    assert event.item_key.value == f"id:{track_id(7)}"
    assert event.item_key.source == "provider_id"
    assert event.title == "Synthetic Song 7"
    assert event.creators == ("Nova Kett",)
    assert event.collection == "Synthetic Album"
    assert event.played_ms == 187_000
    # The export has no track length: never inferred from ms_played.
    assert event.duration_ms is None
    assert event.external_ids == {"spotify_track_id": track_id(7)}
    assert event.provider_extras == {}


def test_the_same_play_gets_the_same_item_key_as_from_the_web_api() -> None:
    from app.spotify.plays import play_from_item

    api = play_from_item(
        {
            "played_at": "2024-03-01T12:34:56.789Z",
            "track": {"id": track_id(7), "name": "Synthetic Song 7", "artists": []},
        }
    )
    mapped = play_from_row(row("2024-03-01T12:34:56Z", 7))

    assert isinstance(mapped, ExportPlay)
    assert mapped.event.item_key == api.event.item_key


def test_a_podcast_episode_is_skipped_as_not_music() -> None:
    assert play_from_row(podcast_row("2024-03-01T12:00:00Z")) is Skip.NOT_MUSIC


def test_an_audiobook_row_is_skipped_as_not_music() -> None:
    audiobook = row(
        "2024-03-01T12:00:00Z",
        spotify_track_uri=None,
        audiobook_title="Synthetic Book",
        audiobook_uri="spotify:audiobook:SynthBook",
    )
    assert play_from_row(audiobook) is Skip.NOT_MUSIC


def test_an_incognito_play_is_skipped() -> None:
    assert play_from_row(row("2024-03-01T12:00:00Z", incognito_mode=True)) is Skip.INCOGNITO


_ABSENT = object()


@pytest.mark.parametrize("value", [_ABSENT, None, "true", "false", 1, 0, True])
def test_a_play_not_marked_incognito_false_is_skipped(value: object) -> None:
    """Fail closed (WHO-106 F4): only an explicit ``false`` is a normal session."""
    data = row("2024-03-01T12:00:00Z")
    if value is _ABSENT:
        del data["incognito_mode"]
    else:
        data["incognito_mode"] = value

    assert play_from_row(data) is Skip.INCOGNITO


def test_a_play_with_incognito_false_is_stored() -> None:
    assert isinstance(play_from_row(row("2024-03-01T12:00:00Z", incognito_mode=False)), ExportPlay)


@pytest.mark.parametrize(("ms_played", "kept"), [(0, False), (29_999, False), (30_000, True)])
def test_plays_under_30_seconds_are_skipped(ms_played: int, kept: bool) -> None:
    mapped = play_from_row(row("2024-03-01T12:00:00Z", ms_played=ms_played))

    assert isinstance(mapped, ExportPlay) is kept
    if not kept:
        assert mapped is Skip.SHORT


@pytest.mark.parametrize(
    "overrides",
    [
        {"ts": None},
        {"ts": "yesterday"},
        # No offset: the zone would be a guess, so the row is not stored.
        {"ts": "2024-03-01T12:00:00"},
        {"ms_played": None},
        {"ms_played": "200000"},
        {"ms_played": True},
        {"ms_played": -1},
        {"master_metadata_track_name": None},
        {"master_metadata_track_name": "  "},
        {"spotify_track_uri": "spotify:episode:SynthEpisode"},
        {"spotify_track_uri": "spotify:track:../../x"},
        {"spotify_track_uri": 42},
    ],
)
def test_a_row_with_a_missing_or_bad_field_is_unreadable(overrides: dict[str, object]) -> None:
    value = row("2024-03-01T12:00:00Z") | overrides

    assert play_from_row(value) is Skip.UNREADABLE


@pytest.mark.parametrize("value", [None, 42, "a string", [1, 2]])
def test_a_row_that_is_not_an_object_is_unreadable(value: object) -> None:
    assert play_from_row(value) is Skip.UNREADABLE


def test_a_timestamp_with_an_offset_is_stored_as_the_same_instant() -> None:
    mapped = play_from_row(row("2024-03-01T14:00:00+02:00"))

    assert isinstance(mapped, ExportPlay)
    assert mapped.event.occurred_at == datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
