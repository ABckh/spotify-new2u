"""SQLAlchemy models.

The unified event table is intentionally absent: its schema is being frozen in a
separate task. Only the declarative base lives here for now.
"""

from app.models.base import Base

__all__ = ["Base"]
