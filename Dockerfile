# One image, two processes: the API runs uvicorn, the poller runs python -m app.poller.
# python:3.12.15-slim-bookworm, pinned by the digest of the multi-arch index.
FROM python@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/srv/src

WORKDIR /srv

# Dependencies first, from the fully pinned lockfile, so a source change does not
# re-resolve or re-download anything.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src

# Run as a non-root user.
# The history-import upload directory (WHO-104): private to the app user. The
# compose volume mounted here starts as a copy of it, owner and mode included.
RUN useradd --system --create-home --uid 10001 app \
    && chown -R app:app /srv \
    && install -d -o app -g app -m 0700 /var/lib/new2u/history-imports
USER app

EXPOSE 8000

# Each compose service sets its own command; this default is the API.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
