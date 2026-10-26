"""ORM metadata without database connections or application configuration."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
