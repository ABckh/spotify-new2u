"""Schema v1: users, the unified event table, collection records, Spotify auth.

``unified_event``, ``ingest_batch`` and ``app_user.home_timezone`` are the
frozen contract: ``schema`` revision 3 on WHO-2, signed off on WHO-4
(confirmation f27c2c23). The DDL below is §5.1 and §5.2 of that document
verbatim, with ``home_timezone`` folded into ``CREATE TABLE app_user`` as the
document says. Do not edit it; a change is a new revision with a new sign-off.

The remaining tables are the auth and connection state of WHO-6. They are not
part of the frozen contract:

- ``spotify_connection``: one per user. Profile fields (WHO-29), the encrypted
  refresh token, when it was authorized (6-month expiry, WHO-20), and the
  poller's liveness fields.
- ``auth_session``: the session tokens we issue to the app, stored as keyed
  hashes. Deleting the row revokes the token.
- ``oauth_login``: one sign-in in progress, between "the app starts the login"
  and "the app claims its session". Linked to no user; deleted once expired.

Revision ID: 0001
Revises:
Create Date: 2026-10-11
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Frozen contract: schema revision 3, §5.1 -------------------------
    op.execute(
        """
        CREATE TABLE app_user (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            home_timezone text NOT NULL DEFAULT 'UTC'
        )
        """
    )
    op.execute(
        """
        CREATE TABLE ingest_batch (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
            source          text NOT NULL,
            source_artifact text NOT NULL,
            ingest_method   text NOT NULL,
            started_at      timestamptz NOT NULL DEFAULT now(),
            finished_at     timestamptz,
            window_start    timestamptz,
            window_end      timestamptz,
            rows_seen       integer NOT NULL DEFAULT 0,
            rows_inserted   integer NOT NULL DEFAULT 0,
            rows_upgraded   integer NOT NULL DEFAULT 0,
            rows_skipped    integer NOT NULL DEFAULT 0,
            status          text NOT NULL DEFAULT 'running',
            error_kind      text,
            CONSTRAINT ingest_batch_status_chk
                CHECK (status IN ('running', 'succeeded', 'failed')),
            CONSTRAINT ingest_batch_counts_chk
                CHECK (rows_seen >= 0 AND rows_inserted >= 0
                       AND rows_upgraded >= 0 AND rows_skipped >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ingest_batch_user_source_started_idx "
        "ON ingest_batch (user_id, source, started_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE unified_event (
            id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id               uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,

            source                text NOT NULL,
            source_artifact       text NOT NULL,
            source_event_id       text,
            ingest_method         text NOT NULL,
            ingest_batch_id       uuid REFERENCES ingest_batch (id) ON DELETE SET NULL,
            ingested_at           timestamptz NOT NULL DEFAULT now(),

            kind                  text NOT NULL,
            occurred_at           timestamptz NOT NULL,
            occurred_at_precision text NOT NULL,
            duration_ms           integer,
            played_ms             integer,

            title                 text NOT NULL,
            creators              text[] NOT NULL DEFAULT '{}',
            collection            text,
            external_ids          jsonb NOT NULL DEFAULT '{}'::jsonb,
            artwork_url           text,
            provider_extras       jsonb NOT NULL DEFAULT '{}'::jsonb,

            item_key              text NOT NULL,
            item_key_source       text NOT NULL,
            dedupe_key            bytea NOT NULL,
            day_key               bytea NOT NULL,
            dedupe_key_version    smallint NOT NULL DEFAULT 1,

            CONSTRAINT unified_event_duration_chk
                CHECK (duration_ms IS NULL OR duration_ms >= 0),
            CONSTRAINT unified_event_played_chk
                CHECK (played_ms IS NULL OR played_ms >= 0),
            CONSTRAINT unified_event_dedupe_key_len_chk
                CHECK (octet_length(dedupe_key) = 32),
            CONSTRAINT unified_event_day_key_len_chk
                CHECK (octet_length(day_key) = 32),
            CONSTRAINT unified_event_title_chk
                CHECK (length(title) > 0),
            CONSTRAINT unified_event_external_ids_obj_chk
                CHECK (jsonb_typeof(external_ids) = 'object'),
            CONSTRAINT unified_event_provider_extras_obj_chk
                CHECK (jsonb_typeof(provider_extras) = 'object')
        )
        """
    )

    # --- Frozen contract: schema revision 3, §5.2 (indexes 6 and 8 removed) -
    op.execute(
        "CREATE UNIQUE INDEX unified_event_user_dedupe_uq ON unified_event (user_id, dedupe_key)"
    )
    op.execute(
        "CREATE INDEX unified_event_timeline_idx "
        "ON unified_event (user_id, occurred_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX unified_event_source_timeline_idx "
        "ON unified_event (user_id, source, occurred_at DESC, id DESC)"
    )
    op.execute("CREATE INDEX unified_event_day_key_idx ON unified_event (user_id, day_key)")
    op.execute(
        "CREATE INDEX unified_event_ingest_batch_idx ON unified_event (ingest_batch_id) "
        "WHERE ingest_batch_id IS NOT NULL"
    )
    op.execute("CREATE INDEX unified_event_creators_idx ON unified_event USING gin (creators)")

    # --- WHO-6 auth and connection state (not part of the frozen contract) --
    op.execute(
        """
        CREATE TABLE spotify_connection (
            user_id                  uuid PRIMARY KEY
                                     REFERENCES app_user (id) ON DELETE CASCADE,
            spotify_account_id       text NOT NULL UNIQUE,
            spotify_display_name     text,
            collection_started_at    timestamptz NOT NULL,
            authorized_at            timestamptz NOT NULL,
            refresh_token_ciphertext bytea,
            status                   text NOT NULL DEFAULT 'active',
            reauth_reason            text,
            last_polled_at           timestamptz,
            CONSTRAINT spotify_connection_status_chk
                CHECK (status IN ('active', 'reauth_required')),
            -- An active connection always has a token; a dead one keeps none.
            CONSTRAINT spotify_connection_token_chk
                CHECK ((status = 'active') = (refresh_token_ciphertext IS NOT NULL))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auth_session (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id           uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
            login_id          uuid NOT NULL UNIQUE,
            claim_secret_hash bytea,
            token_hash        bytea UNIQUE,
            created_at        timestamptz NOT NULL DEFAULT now(),
            expires_at        timestamptz NOT NULL,
            -- Unclaimed: only the claim secret hash. Claimed: only the token hash.
            CONSTRAINT auth_session_one_secret_chk
                CHECK ((claim_secret_hash IS NULL) <> (token_hash IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX auth_session_user_idx ON auth_session (user_id)")
    op.execute(
        """
        CREATE TABLE oauth_login (
            id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            state_hash               bytea NOT NULL UNIQUE,
            code_verifier_ciphertext bytea NOT NULL,
            claim_secret_hash        bytea NOT NULL,
            home_timezone            text NOT NULL,
            created_at               timestamptz NOT NULL DEFAULT now(),
            expires_at               timestamptz NOT NULL,
            outcome                  text NOT NULL DEFAULT 'pending',
            failure_kind             text,
            CONSTRAINT oauth_login_outcome_chk
                CHECK (outcome IN ('pending', 'callback_used', 'connected', 'failed'))
        )
        """
    )
    op.execute("CREATE INDEX oauth_login_expires_idx ON oauth_login (expires_at)")


def downgrade() -> None:
    for table in (
        "oauth_login",
        "auth_session",
        "spotify_connection",
        "unified_event",
        "ingest_batch",
        "app_user",
    ):
        op.execute(f"DROP TABLE {table}")
