"""Shared SQLAlchemy declarative base.

Table modules import Base from here so ``mma_model.db.models`` can re-export
canonical tables without a core↔models import cycle.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
