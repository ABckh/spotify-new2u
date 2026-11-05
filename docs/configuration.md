# Configuration

Every setting comes from the environment; see `.env.example` for the full list
with comments. `.env` is git-ignored and must never be committed — no client id,
client secret, refresh token or session secret belongs in this repo.

Two notes on the config, because they are easy to get wrong:

- `DATABASE_URL` is the only place the connection string is defined.
  `alembic.ini` leaves `sqlalchemy.url` empty and `alembic/env.py` reads it from
  the settings, so no credential can end up in a tracked file.
- Spotify auth is Authorization Code with **PKCE**. `SPOTIFY_CLIENT_SECRET` is
  optional: when set, token requests also authenticate with it. Refresh tokens
  stay server-side, encrypted at rest (AES-256-GCM) with `TOKEN_ENCRYPTION_KEY`;
  the device only ever gets our own tokens: a short-lived session token and a
  refresh token that renews it (see
  [How it works](how-it-works.md#sign-in-and-collection)).
- `SPOTIFY_REDIRECT_URI` is sent to Spotify verbatim and must match the
  registered value character for character. Locally it is
  `http://127.0.0.1:8000/auth/spotify/callback` (Spotify rejects `localhost`).
- `APP_CALLBACK_URL` is the app's own custom scheme (`new2u://signed-in`), with
  no query or fragment; `http`, `https` and other web schemes are refused, so
  the one-time callback code never goes to a web page. It is required unless
  `APP_ENV=local` **and** `SPOTIFY_REDIRECT_URI` is on `127.0.0.1`, `localhost`
  or `::1`; otherwise the API refuses to start. Locally it may be empty, and the
  callback page then shows the callback code so you can claim by hand.
- `SPOTIFY_ALLOWED_ACCOUNT_IDS` lists the Spotify accounts that may sign in,
  comma-separated. Any other account fails with
  `account_not_allowed` and nothing is stored: no user, no connection, no
  session. Renewals are checked too. With the list empty, `APP_ENV=local` lets
  every account in; staging and production let **no** account in and log a
  warning at startup. To find an account ID: in the Spotify app, open the
  profile, then `...` > Share > Copy link to profile; the ID is the part of
  `https://open.spotify.com/user/<id>?si=...` between `/user/` and `?`. A signed-in
  user also sees it as `spotify_account_id` on `GET /v1/me/connection`. The list
  is read at startup: after a change run `docker compose up -d` (add the
  `-f` files on the VPS), which recreates the containers; `docker compose restart`
  keeps the old value. Taking an ID off the list stops new sign-ins and
  renewals for it, but not the polling of a connection that already exists;
  to stop that, delete the account (`DELETE /v1/me`).
- The sign-in routes are rate limited per client IP (`LOGIN_RATE_LIMIT_PER_MINUTE`,
  `CLAIM_RATE_LIMIT_PER_MINUTE`), counted in the API process. Behind a reverse
  proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address as the `api` container
  sees it, so uvicorn (`--proxy-headers`) takes the client IP from
  `X-Forwarded-For`. Leave it at `127.0.0.1` with no proxy. Never use `*`.
  IPv6 clients are counted per /64. The counter table is capped at 10,000
  entries; when it is full of running windows, new clients get `429` until a
  window ends, instead of the table growing.
