# Development

## Layout

```
src/app/api/      FastAPI app (one process)
src/app/auth/     sign-in with Spotify (PKCE), session tokens
src/app/events/   the unified event table: dedupe key recipe and the one writer
src/app/spotify/  Spotify Web API client, play mapping, connection store
src/app/poller/   history poller (a separate process)
src/app/privacy/  field list, delete path, retention
src/app/read/     the read API's queries: timeline, albums, new to you, ratings
src/app/core/     settings, database engine, logging, crypto helpers
alembic/          migrations; 0001 is schema v1
tests/            pytest suite
deploy/           server setup and release scripts, Caddy, systemd units
scripts/          run from your machine: bootstrap, deploy, iPhone install
ios/              the iPhone app (SwiftUI)
docs/             these guides
```

## Run it locally

```sh
cp .env.example .env            # then edit: generate the two keys it points at
docker compose up --build
curl -s http://127.0.0.1:8000/healthz   # -> {"status":"ok"}
docker compose run --rm api alembic upgrade head
```

`docker compose up` starts three services: `postgres`, `api` (uvicorn) and
`poller` (`python -m app.poller`). The poller is a separate container on purpose:
restarting or breaking the API must never stop ingestion.

Stop with `docker compose down`; add `-v` to also drop the Postgres volume.

### Try the API in Swagger

With `APP_ENV=local`, Swagger UI is at <http://127.0.0.1:8000/docs> and the
OpenAPI schema at `/openapi.json`. To call the authenticated routes:

1. `POST /auth/spotify/login` with `{"home_timezone": "UTC"}`; open the
   returned `authorize_url` in a browser and sign in to Spotify.
2. With `APP_CALLBACK_URL` empty, the callback page shows the callback code.
   `POST /auth/spotify/login/{login_id}/claim` with the claim secret and that code.
3. Press **Authorize**, paste the `session_token` (just the token, no
   `Bearer `), and every `/v1` route sends it.

While authorized, `POST /auth/spotify/login` sends the token too and starts a
renewal for that account; log out of **Authorize** to start a fresh sign-in.
The page loads its scripts from the jsDelivr CDN, so the browser needs internet.

On a server the docs are off: `/docs` and `/openapi.json` answer `404`.
`API_DOCS_ENABLED=true` turns them on (for staging, say); keep it unset in
production.

## Run the checks

```sh
python3.12 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip-audit -r requirements.lock --require-hashes --strict --disable-pip
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

`pytest` and `mypy` read the package straight out of `src/` (`pythonpath` /
`mypy_path` in `pyproject.toml`), so there is no editable install step.

To run the migration check against the compose database:

```sh
DATABASE_URL=postgresql+psycopg://new2u:change-me@127.0.0.1:5432/new2u .venv/bin/alembic upgrade head
```

CI (`.github/workflows/ci.yml`) runs exactly these: `pip-audit` (on both lock
files), `ruff`, `mypy`, `pytest`, and `alembic upgrade head` against a Postgres
service container. A new advisory against a pinned version fails CI until the
pin is bumped.

## Dependencies

Direct dependencies are pinned exactly in `pyproject.toml`. The full transitive
set — with hashes — is in `requirements.lock` (runtime, what the image installs)
and `requirements-dev.lock` (runtime plus `ruff`, `mypy`, `pytest`). Regenerate
after changing `pyproject.toml`:

```sh
uv pip compile pyproject.toml --universal --generate-hashes --python-version 3.12 -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --generate-hashes --python-version 3.12 -o requirements-dev.lock
```
