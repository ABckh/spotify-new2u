"""The one-time Spotify extended history import (WHO-94, WHO-104).

- ``POST /v1/spotify/history-import`` (Bearer), ``multipart/form-data`` with one
  file part named ``file``: the ``my_spotify_data.zip`` Spotify sent, or one
  ``Streaming_History_Audio_*.json`` from it. The body is streamed straight to a
  private temp file (0600); the zip's entry list is checked; the import is
  queued. ``202 {"id", "status": "queued"}``. The poller's import worker does
  the rest; poll the status route for the outcome.
- ``GET /v1/spotify/history-import`` (Bearer) -> ``{"latest": null}`` or the
  latest import: ``id``, ``status`` (``queued | running | succeeded |
  failed``), ``error_kind``, ``created_at``, ``started_at``, ``finished_at``
  and ``counts`` (``rows_seen``, ``rows_inserted``, ``rows_duplicate``,
  ``rows_skipped_not_music``, ``rows_skipped_incognito``, ``rows_skipped_short``,
  ``rows_unreadable``). Counts grow while the import runs.

Errors are ``{"detail": {"code", "message"}}``:

- ``409 history_import_in_progress``: one is queued or running. Checked before
  the body is read. ``409 history_import_already_done``: one succeeded; the
  import is one time per user. A failed import does not count.
- ``413 upload_too_large``: over 200 MB.
- ``415 not_multipart``: not ``multipart/form-data``.
- ``422``: ``missing_file``, ``too_many_files``, ``invalid_multipart``,
  ``unsupported_file`` (neither a zip nor a JSON array), ``no_streaming_history``
  (a zip without ``Streaming_History_Audio_*.json``), ``entry_too_large``
  (an audio entry or the JSON file over 50 MB), ``zip_too_many_entries``
  (over 100), ``zip_too_large`` (over 1 GB uncompressed),
  ``zip_ratio_exceeded`` (an entry compressed over 100:1), ``zip_unsafe_path``,
  ``zip_nested_archive``, ``zip_encrypted``.

A rejected upload stores nothing: the temp file is deleted before the answer.
A failed job's ``error_kind`` uses the same codes, plus ``invalid_json``,
``too_many_rows`` (over 1,000,000), ``no_music_plays`` (every row was a
podcast, a private session, a short play or unreadable), ``upload_missing``,
``interrupted`` and ``internal_error``.

Every upload first deletes upload files older than 23 h, as the poller does
hourly, so no file outlives 24 h while the poller is down.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy import Engine
from starlette.concurrency import run_in_threadpool

from app.api.auth import EngineDep, NowDep, UserDep
from app.api.rate_limit import check_per_user
from app.spotify import history_import
from app.spotify.extended_history import MAX_UPLOAD_BYTES, FileKind, ImportRejected, inspect_upload

router = APIRouter(prefix="/v1/spotify", tags=["collection"])

# Uploads are heavy; a person uploads once. A few retries a minute is plenty.
UPLOADS_PER_MINUTE: Final = 5
# Boundaries and part headers around the one file.
_MULTIPART_OVERHEAD: Final = 64 * 1024

_MESSAGES: Final[dict[str, str]] = {
    "history_import_in_progress": "Your Spotify history import is already running.",
    "history_import_already_done": "Your full Spotify history is already imported.",
    "upload_too_large": "The file is larger than 200 MB.",
    "not_multipart": "Send the file as multipart/form-data.",
    "missing_file": "Attach the file Spotify sent you as the 'file' field.",
    "too_many_files": "Send one file.",
    "invalid_multipart": "The upload could not be read.",
    "unsupported_file": "This is not a Spotify streaming history export (.zip or .json).",
    "no_streaming_history": "The zip has no Streaming_History_Audio files.",
    "entry_too_large": "A streaming history file is larger than 50 MB.",
    "zip_too_many_entries": "The zip has more than 100 files.",
    "zip_too_large": "The zip unpacks to more than 1 GB.",
    "zip_ratio_exceeded": "The zip is compressed too heavily to be a Spotify export.",
    "zip_unsafe_path": "The zip has a file outside its own folder.",
    "zip_nested_archive": "The zip holds another archive.",
    "zip_encrypted": "The zip is encrypted.",
}


def _error(http_status: int, code: str) -> HTTPException:
    return HTTPException(http_status, detail={"code": code, "message": _MESSAGES[code]})


def _conflict(blocking: str) -> HTTPException:
    if blocking == "succeeded":
        return _error(status.HTTP_409_CONFLICT, "history_import_already_done")
    return _error(status.HTTP_409_CONFLICT, "history_import_in_progress")


class ImportAccepted(BaseModel):
    id: UUID
    status: str


class ImportCountsOut(BaseModel):
    rows_seen: int
    rows_inserted: int
    rows_duplicate: int
    rows_skipped_not_music: int
    rows_skipped_incognito: int
    rows_skipped_short: int
    rows_unreadable: int


class HistoryImportOut(BaseModel):
    id: UUID
    status: str
    error_kind: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    counts: ImportCountsOut


class LatestImport(BaseModel):
    latest: HistoryImportOut | None


@router.get("/history-import", response_model=LatestImport)
def get_history_import(user_id: UserDep, engine: EngineDep) -> LatestImport:
    with engine.connect() as conn:
        latest = history_import.latest_import(conn, user_id)
    if latest is None:
        return LatestImport(latest=None)
    return LatestImport(
        latest=HistoryImportOut(
            id=latest.id,
            status=latest.status,
            error_kind=latest.error_kind,
            created_at=latest.created_at,
            started_at=latest.started_at,
            finished_at=latest.finished_at,
            counts=ImportCountsOut(**latest.counts.__dict__),
        )
    )


@router.post(
    "/history-import",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ImportAccepted,
    responses={409: {}, 413: {}, 415: {}, 422: {}, 429: {}},
)
async def upload_history_import(
    request: Request, user_id: UserDep, engine: EngineDep, now: NowDep
) -> ImportAccepted:
    check_per_user("history_import_upload", user_id, UPLOADS_PER_MINUTE)
    await run_in_threadpool(history_import.sweep_old_files)
    blocking = await run_in_threadpool(_blocking_status, engine, user_id)
    if blocking is not None:
        raise _conflict(blocking)

    content_type, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise _error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "not_multipart")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD:
        raise _error(status.HTTP_413_CONTENT_TOO_LARGE, "upload_too_large")

    import_id = uuid4()
    partial = history_import.partial_path(import_id)
    final = history_import.upload_path(import_id)
    queued = False
    try:
        await _receive_file(request, boundary, partial)
        try:
            kind: FileKind = await run_in_threadpool(inspect_upload, partial)
        except ImportRejected as exc:
            code = exc.code
            if code == "upload_too_large":
                raise _error(status.HTTP_413_CONTENT_TOO_LARGE, code) from None
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, code) from None
        # Renamed before the row exists, so the worker never claims an import
        # whose file is still being written.
        partial.rename(final)
        blocking = await run_in_threadpool(_create, engine, import_id, user_id, kind, now)
        if blocking is not None:
            raise _conflict(blocking)
        queued = True
    finally:
        partial.unlink(missing_ok=True)
        if not queued:
            final.unlink(missing_ok=True)
    return ImportAccepted(id=import_id, status="queued")


def _blocking_status(engine: Engine, user_id: UUID) -> str | None:
    with engine.connect() as conn:
        return history_import.blocking_status(conn, user_id)


def _create(
    engine: Engine, import_id: UUID, user_id: UUID, kind: FileKind, now: datetime
) -> str | None:
    with engine.begin() as conn:
        return history_import.create_import(conn, import_id, user_id, kind, now)


class _FilePart:
    """Multipart callbacks that write the one ``file`` part to a private file."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.files = 0
        self.file_bytes = 0
        self._in_file = False
        self._header_field = b""
        self._header_value = b""
        self._is_file_part = False

    def on_part_begin(self) -> None:
        self._is_file_part = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        if self._header_field.lower() == b"content-disposition":
            _, params = parse_options_header(self._header_value)
            self._is_file_part = params.get(b"name") == b"file"
        self._header_field = b""
        self._header_value = b""

    def on_headers_finished(self) -> None:
        if self._is_file_part:
            self.files += 1
            if self.files > 1:
                raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "too_many_files")
        self._in_file = self._is_file_part

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if not self._in_file:
            return
        self.file_bytes += end - start
        if self.file_bytes > MAX_UPLOAD_BYTES:
            raise _error(status.HTTP_413_CONTENT_TOO_LARGE, "upload_too_large")
        os.write(self.fd, data[start:end])

    def on_part_end(self) -> None:
        self._in_file = False


async def _receive_file(request: Request, boundary: bytes, dest: Path) -> None:
    """Stream the body into ``dest`` (0600), keeping only the ``file`` part."""
    fd = history_import.open_private(dest)
    try:
        part = _FilePart(fd)
        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": part.on_part_begin,
                "on_header_field": part.on_header_field,
                "on_header_value": part.on_header_value,
                "on_header_end": part.on_header_end,
                "on_headers_finished": part.on_headers_finished,
                "on_part_data": part.on_part_data,
                "on_part_end": part.on_part_end,
            },
        )
        body_bytes = 0
        try:
            async for chunk in request.stream():
                body_bytes += len(chunk)
                if body_bytes > MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD:
                    raise _error(status.HTTP_413_CONTENT_TOO_LARGE, "upload_too_large")
                parser.write(chunk)
            parser.finalize()
        except MultipartParseError:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_multipart") from None
        if part.files == 0:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "missing_file")
    finally:
        os.close(fd)
