"""Declarative base shared by every model.

Alembic autogenerate compares the database against ``Base.metadata``, so every
model module must be imported from ``app.models`` for it to be visible.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
