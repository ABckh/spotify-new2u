"""Alembic environment.

The database URL comes from app.core.config (i.e. from DATABASE_URL), never from
alembic.ini, so there is one source of truth and no credential in a tracked file.

``alembic upgrade head`` is expected to succeed with zero revision files present:
it creates alembic_version and stops.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.core.config import get_settings
from app.core.db import UTC_SESSION
from app.models import Base

config = context.config

if config.config_file_name is not None:
    # Keep loggers that already exist: when migrations run in-process (the test
    # suite does), the default would silence app.privacy.delete's account_deleted line.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate compares against this. Model modules must be imported by
# app.models for their tables to appear here.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it against a database."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database.

    The migration tests pass ``url`` in ``config.attributes`` to migrate a
    throwaway database; everything else uses DATABASE_URL.
    """
    url = config.attributes.get("url") or get_settings().database_url
    engine = create_engine(url, poolclass=None, connect_args=UTC_SESSION)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
