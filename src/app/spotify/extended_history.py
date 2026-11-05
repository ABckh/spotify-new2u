"""Read the Spotify "Extended streaming history" export safely (WHO-94 decisions 3, 5, 7, 8).

The user uploads the file Spotify sent, as is: ``my_spotify_data.zip``, or one
``Streaming_History_Audio_*.json`` from it. Only entries named
``Streaming_History_Audio_*.json`` are read; every other entry of the zip
(``Userdata.json``, payments, video history, ...) is never opened.

Two stages, both raising :class:`ImportRejected` with an error code and
nothing else:

- :func:`inspect_upload` runs in the upload request. It only reads the zip's
  central directory (or the first bytes of a JSON file): entry count, declared
  sizes, compression ratio, names, nested archives.
- :func:`iter_rows` runs in the import job. It decompresses each audio entry
  through a counter, so a zip that lies about its sizes is still stopped at the
  limit, and parses it one row at a time: a 50 MB entry is never held as one
  decoded document.

:func:`play_from_row` maps one row to a ``PreciseEvent`` or says why it is
skipped. It reads only the fields of decision 8. ``ip_addr``,
``conn_country``, ``platform``, ``user_agent_decrypted``, ``offline*``,
``shuffle``, ``skipped``, ``reason_*`` and ``username`` are never read.

Nothing here logs. Error codes, reasons and counts are the only output; the
file's content never reaches a log line or an exception message.
"""

import codecs
import json
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import IO, Final, Literal

from app.events import keys
from app.events.store import PreciseEvent
from app.spotify.plays import SOURCE

SOURCE_ARTIFACT: Final = "spotify_extended_history"

# Decision 5. Changing a limit here changes what the API accepts.
MAX_UPLOAD_BYTES: Final = 200 * 1024 * 1024
MAX_ZIP_ENTRIES: Final = 100
MAX_ZIP_UNCOMPRESSED_BYTES: Final = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 100
MAX_ENTRY_BYTES: Final = 50 * 1024 * 1024
MAX_ROWS: Final = 1_000_000
# One export row is a few hundred bytes. A "row" this large is not one.
MAX_ROW_BYTES: Final = 64 * 1024

# Decision 7: what the Web API counts as a play. Product may change it.
MIN_PLAYED_MS: Final = 30_000

FileKind = Literal["zip", "json"]

_AUDIO_ENTRY = re.compile(r"Streaming_History_Audio_[^/\\]*\.json")
_ARCHIVE_SUFFIXES = (".zip", ".jar", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz")
_ZIP_MAGIC = b"PK\x03\x04"
_TRACK_URI = re.compile(r"spotify:track:([0-9A-Za-z]{1,64})")
_READ_CHUNK = 64 * 1024


class ImportRejected(ValueError):
    """The upload breaks a rule. ``code`` is the API error code; it names no content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_audio_entry(name: str) -> bool:
    """Whether a zip entry is one we read: its file name, in any folder."""
    return _AUDIO_ENTRY.fullmatch(PurePosixPath(name).name) is not None


def inspect_upload(path: Path) -> FileKind:
    """The cheap checks of the upload request. Returns the file kind or raises."""
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise ImportRejected("upload_too_large")
    with path.open("rb") as f:
        head = f.read(_READ_CHUNK)
    if head.startswith(_ZIP_MAGIC):
        _audio_entries(path)
        return "zip"
    if head.removeprefix(b"\xef\xbb\xbf").lstrip()[:1] == b"[":
        if size > MAX_ENTRY_BYTES:
            raise ImportRejected("entry_too_large")
        return "json"
    raise ImportRejected("unsupported_file")


def _audio_entries(path: Path) -> list[zipfile.ZipInfo]:
    """Check the central directory; return the audio entries to read, in name order."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, EOFError):
        raise ImportRejected("unsupported_file") from None
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ImportRejected("zip_too_many_entries")
    if sum(i.file_size for i in infos) > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ImportRejected("zip_too_large")
    audio: list[zipfile.ZipInfo] = []
    for info in infos:
        _check_entry_name(info.filename)
        if info.flag_bits & 0x1:
            raise ImportRejected("zip_encrypted")
        if info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1):
            raise ImportRejected("zip_ratio_exceeded")
        if info.is_dir():
            continue
        if is_audio_entry(info.filename):
            if info.file_size > MAX_ENTRY_BYTES:
                raise ImportRejected("entry_too_large")
            audio.append(info)
    if not audio:
        raise ImportRejected("no_streaming_history")
    return sorted(audio, key=lambda i: i.filename)


def _check_entry_name(name: str) -> None:
    # Entries are never extracted to disk, so a traversal could not escape
    # anyway. A zip that tries is still not a Spotify export: reject it.
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise ImportRejected("zip_unsafe_path")
    parts = PurePosixPath(name).parts
    if ".." in parts or (parts and parts[0].endswith(":")):
        raise ImportRejected("zip_unsafe_path")
    if name.lower().endswith(_ARCHIVE_SUFFIXES):
        raise ImportRejected("zip_nested_archive")


class _Counted:
    """A binary reader that stops at ``limit`` decompressed bytes."""

    def __init__(self, raw: IO[bytes], limit: int) -> None:
        self._raw = raw
        self._limit = limit
        self.read_bytes = 0

    def read(self, n: int) -> bytes:
        data = self._raw.read(n)
        self.read_bytes += len(data)
        if self.read_bytes > self._limit:
            raise ImportRejected("entry_too_large")
        return data


def iter_rows(path: Path, kind: FileKind) -> Iterator[object]:
    """Every row of every audio entry, one at a time. Enforces the job's limits.

    Raises :class:`ImportRejected` when an entry breaks a limit or is not a JSON
    array. A row that is valid JSON but not an export row is yielded as is;
    :func:`play_from_row` counts it as unreadable.
    """
    rows = 0
    for value in _iter_values(path, kind):
        rows += 1
        if rows > MAX_ROWS:
            raise ImportRejected("too_many_rows")
        yield value


def _iter_values(path: Path, kind: FileKind) -> Iterator[object]:
    if kind == "json":
        with path.open("rb") as f:
            yield from _json_array(_Counted(f, MAX_ENTRY_BYTES))
        return
    entries = _audio_entries(path)
    total = 0
    try:
        with zipfile.ZipFile(path) as zf:
            for info in entries:
                with zf.open(info) as raw:
                    counted = _Counted(raw, MAX_ENTRY_BYTES)
                    yield from _json_array(counted)
                    total += counted.read_bytes
                    if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                        raise ImportRejected("zip_too_large")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
        raise ImportRejected("unsupported_file") from None
    except OSError:
        # zipfile reports a bad CRC or a short entry as an OSError subclass.
        raise ImportRejected("unsupported_file") from None


def _json_array(stream: _Counted) -> Iterator[object]:
    """The elements of one top-level JSON array, decoded one at a time."""
    yield from _ArrayReader(stream).elements()


class _ArrayReader:
    """Decodes ``[v, v, ...]`` from a stream, holding at most a row or two of text.

    ``raw_decode`` is tried on the buffered text; while the element is cut off
    at the end of the buffer, more is read. A value that ends exactly at the end
    of the buffer is read past too, so a number split across two reads is never
    taken for a shorter one. Bytes that are not UTF-8 reject the entry.
    """

    def __init__(self, stream: _Counted) -> None:
        self._stream = stream
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._decoder = json.JSONDecoder()
        self._buf = ""
        self._pos = 0
        self._eof = False

    def elements(self) -> Iterator[object]:
        self._fill()
        if self._buf.startswith("\ufeff"):
            self._pos = 1
        if self._next_char() != "[":
            raise ImportRejected("invalid_json")
        self._pos += 1
        if self._next_char() == "]":
            self._pos += 1
            self._expect_end()
            return
        while True:
            yield self._value()
            separator = self._next_char()
            self._pos += 1
            if separator == "]":
                self._expect_end()
                return
            if separator != ",":
                raise ImportRejected("invalid_json")

    def _value(self) -> object:
        self._next_char()
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buf, self._pos)
            except json.JSONDecodeError:
                if len(self._buf) - self._pos > MAX_ROW_BYTES or not self._fill():
                    raise ImportRejected("invalid_json") from None
                continue
            if end == len(self._buf) and self._fill():
                continue
            if end - self._pos > MAX_ROW_BYTES:
                raise ImportRejected("invalid_json")
            self._pos = end
            return value

    def _next_char(self) -> str:
        """Skip whitespace; the next character, or "" at the end of the stream."""
        while True:
            while self._pos < len(self._buf) and self._buf[self._pos] in " \t\r\n":
                self._pos += 1
            if self._pos < len(self._buf):
                return self._buf[self._pos]
            if not self._fill():
                return ""

    def _expect_end(self) -> None:
        if self._next_char() != "":
            raise ImportRejected("invalid_json")

    def _fill(self) -> bool:
        """Read one more chunk; False at the end of the stream."""
        if self._eof:
            return False
        data = self._stream.read(_READ_CHUNK)
        try:
            text = self._utf8.decode(data, final=not data)
        except UnicodeDecodeError:
            raise ImportRejected("invalid_json") from None
        if not data:
            self._eof = True
        self._buf = self._buf[self._pos :] + text
        self._pos = 0
        return bool(data)


class Skip(StrEnum):
    """Why a row is not stored (decision 7)."""

    NOT_MUSIC = "not_music"  # podcast episode, audiobook, video: no track URI
    INCOGNITO = "incognito"  # the user chose a private session
    SHORT = "short"  # under MIN_PLAYED_MS: not a play for the Web API either
    UNREADABLE = "unreadable"  # a field we need is missing or malformed


@dataclass(frozen=True)
class ExportPlay:
    event: PreciseEvent


def play_from_row(row: object) -> ExportPlay | Skip:
    """Map one export row, or say why it is skipped. Reads only the fields of decision 8."""
    if not isinstance(row, dict):
        return Skip.UNREADABLE
    uri = row.get("spotify_track_uri")
    if uri is None or uri == "":
        return Skip.NOT_MUSIC
    # Fail closed: only an explicit ``false`` is a normal session. Missing,
    # null or any other value is treated as private (WHO-106 F4).
    if row.get("incognito_mode") is not False:
        return Skip.INCOGNITO
    if not isinstance(uri, str):
        return Skip.UNREADABLE
    match = _TRACK_URI.fullmatch(uri)
    if match is None:
        return Skip.UNREADABLE
    ms_played = row.get("ms_played")
    if not isinstance(ms_played, int) or isinstance(ms_played, bool) or ms_played < 0:
        return Skip.UNREADABLE
    if ms_played < MIN_PLAYED_MS:
        return Skip.SHORT
    occurred_at = _ts(row.get("ts"))
    title = row.get("master_metadata_track_name")
    if occurred_at is None or not isinstance(title, str) or not title.strip():
        return Skip.UNREADABLE
    artist = row.get("master_metadata_album_artist_name")
    album = row.get("master_metadata_album_album_name")
    track_id = match.group(1)
    return ExportPlay(
        PreciseEvent(
            source=SOURCE,
            source_artifact=SOURCE_ARTIFACT,
            ingest_method="file_import",
            kind="listen",
            # End of play, as the Web API's played_at (decision 8).
            occurred_at=occurred_at,
            title=title,
            item_key=keys.item_key_from_provider_id(track_id),
            # The export has no track length; never inferred from ms_played.
            duration_ms=None,
            # Spotify measured it, so it is stored (decision 8).
            played_ms=ms_played,
            creators=(artist,) if isinstance(artist, str) and artist else (),
            collection=album if isinstance(album, str) and album else None,
            external_ids={"spotify_track_id": track_id},
        )
    )


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Spotify writes "...Z". Without an offset the zone would be a guess.
    if ts.tzinfo is None or ts.utcoffset() is None:
        return None
    return ts
