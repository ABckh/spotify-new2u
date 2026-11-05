"""Application settings.

Every setting is read from the environment (or a local .env file). Nothing is
inferred and nothing has a secret default: if a secret is missing, startup fails
loudly rather than running with a placeholder.
"""

import re
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# APP_CALLBACK_URL must be an app's custom scheme (WHO-40 N2): a scheme, "://"
# and something after it, no whitespace. Schemes a browser would open itself
# are refused, so the one-time callback code never lands on a web page.
_CUSTOM_SCHEME_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+")
_BROWSER_SCHEMES = frozenset(
    {"http", "https", "ws", "wss", "ftp", "file", "javascript", "data", "blob"}
)
# Hosts on which the callback page may show the callback code (WHO-40 N1).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_env: Literal["local", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    api_port: int = 8000
    # Swagger UI at /docs and the schema at /openapi.json, for trying the API by
    # hand (WHO-58). Unset: on when APP_ENV=local, off otherwise, so a server
    # does not publish its route list unless someone turns it on.
    api_docs_enabled: bool | None = None

    # Database
    database_url: str

    # Secrets. No defaults on purpose.
    token_encryption_key: str
    session_token_secret: str
    # One hour; at most a day (WHO-36 F6). 0, negative or 30 days are refused.
    session_token_ttl_seconds: int = Field(default=3600, gt=0, le=86400)
    # How long a refresh token works without use: 90 days, at most a year. Each
    # POST /auth/refresh rotates it and starts the time again, so only that
    # long without opening the app means signing in with Spotify again.
    refresh_token_ttl_seconds: int = Field(default=90 * 86400, gt=0, le=365 * 86400)

    # Spotify: Authorization Code with PKCE. The PKCE verifier is always sent.
    # The client secret is optional: when set, the token requests also
    # authenticate with HTTP Basic (a confidential client); when empty, the app
    # is a public client. SPOTIFY_REDIRECT_URI is sent verbatim in the authorize
    # URL and in the token exchange; it is never rebuilt from the request host.
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = ""
    # Sign-up allowlist (WHO-36 F3, owner's decision on WHO-38): the Spotify
    # account IDs that may sign in, comma-separated. Empty means any account
    # with APP_ENV=local and no account anywhere else. Read once at startup.
    spotify_allowed_account_ids: str = ""

    # Sign-in. How long the app has to finish a sign-in it started, and the app
    # URL the callback redirects to when it is done, with the login id, the
    # result and the one-time callback code the claim needs (WHO-36 F1).
    # Required outside local: without it no device can claim a sign-in.
    oauth_login_ttl_seconds: int = Field(default=600, gt=0)
    app_callback_url: str = ""
    # Abuse limits on the unauthenticated sign-in routes (WHO-36 F2): requests
    # per client IP per minute, and sign-ins in progress across all clients.
    login_rate_limit_per_minute: int = Field(default=10, gt=0)
    claim_rate_limit_per_minute: int = Field(default=60, gt=0)
    max_pending_logins: int = Field(default=500, gt=0)
    # Per-user limits on the authenticated reads (WHO-7), requests per minute.
    # Export reads every row the user has, so it gets far fewer.
    read_rate_limit_per_minute: int = Field(default=120, gt=0)
    export_rate_limit_per_minute: int = Field(default=2, gt=0)
    # An export holds a DB connection and a snapshot while the client reads it.
    # Postgres ends one that waits on a stalled reader this long (WHO-48 F1).
    export_idle_timeout_seconds: int = Field(default=60, gt=0)

    # Poller: every 15 minutes. Spotify only lists the last 50 plays, so this
    # must stay far below the time 50 tracks take.
    poller_interval_seconds: int = Field(default=900, gt=0)
    # Between ticks, how often the poller looks for a connection it has not
    # polled since the user connected, and polls it at once (WHO-87).
    poller_new_connection_check_seconds: int = Field(default=15, gt=0)
    # A manual poll (POST /v1/me/spotify/poll, WHO-95) is refused this long after
    # the last poll attempt or manual request. Spotify lists only 50 plays, so
    # polling more often finds almost nothing and spends our shared rate limit.
    manual_poll_cooldown_seconds: int = Field(default=300, gt=0)
    # Warn this many days before the Spotify grant's 6-month expiry (WHO-20).
    reauth_warning_days: int = Field(default=30, gt=0)

    # History import (WHO-104). Where an uploaded export waits for the import
    # job: a private directory (0700, files 0600) the API and the poller both
    # see. Empty means "new2u-history-imports" in the system temp directory,
    # which only works when both run on one host; compose sets a shared volume.
    history_import_dir: str = ""
    # How often the poller's import worker looks for a queued import.
    history_import_check_seconds: int = Field(default=5, gt=0)

    # Retention (PRD P10). How long collection records (ingest_batch rows) are
    # kept: 90 days by default.
    collection_records_retention_days: int = Field(default=90, gt=0)

    @property
    def spotify_account_allowlist(self) -> frozenset[str] | None:
        """The Spotify account IDs that may sign in; ``None`` when any account may.

        Spaces around an ID are trimmed and empty items dropped. An empty list
        lets every account in only with APP_ENV=local; in staging and production
        it lets none in, so a missing setting fails closed.
        """
        ids = {item.strip() for item in self.spotify_allowed_account_ids.split(",")}
        ids.discard("")
        if not ids and self.app_env == "local":
            return None
        return frozenset(ids)

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is None:
            return self.app_env == "local"
        return self.api_docs_enabled

    @model_validator(mode="after")
    def _app_callback_url_outside_local(self) -> Self:
        if self.app_callback_url:
            _check_app_callback_url(self.app_callback_url)
            return self
        if self.app_env != "local":
            raise ValueError("APP_CALLBACK_URL is required when APP_ENV is not local")
        # Without APP_CALLBACK_URL the callback page shows the callback code in
        # whatever browser finished the sign-in. Allow that only when Spotify
        # sends the browser back to this machine (WHO-40 N1), so a server whose
        # .env left out APP_ENV or kept APP_ENV=local refuses to start instead.
        # An empty SPOTIFY_REDIRECT_URI is allowed: no sign-in can start without
        # one (the poller and the CI migration step run without it).
        if self.spotify_redirect_uri:
            host = urlsplit(self.spotify_redirect_uri).hostname
            if host not in _LOOPBACK_HOSTS:
                raise ValueError(
                    "APP_CALLBACK_URL is required unless SPOTIFY_REDIRECT_URI is on "
                    "127.0.0.1, localhost or ::1"
                )
        return self


def _check_app_callback_url(url: str) -> None:
    if "?" in url or "#" in url:
        raise ValueError("APP_CALLBACK_URL must not carry a query or a fragment")
    if not _CUSTOM_SCHEME_URL.fullmatch(url):
        raise ValueError("APP_CALLBACK_URL must be an app URL such as new2u://signed-in")
    if url.split(":", 1)[0] in _BROWSER_SCHEMES:
        raise ValueError("APP_CALLBACK_URL must use the app's own scheme, not a web scheme")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
