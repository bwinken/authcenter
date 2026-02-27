"""SQLAlchemy models for both databases."""

from datetime import datetime

from sqlalchemy import String, Integer, Text, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# --- SQLite Models (Auth Local DB) ---

class SQLiteBase(DeclarativeBase):
    pass


class UserAccount(SQLiteBase):
    __tablename__ = "user_accounts"

    staff_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AppAccessRule(SQLiteBase):
    __tablename__ = "app_access_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    allowed_depts: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of dept_codes
    min_level: Mapped[int] = mapped_column(Integer, default=1)


# --- MySQL model (IT Master DB - read only, reflected) ---
# We use raw SQL queries for the MySQL staff table to avoid
# needing a separate DeclarativeBase bound to the MySQL engine.
# The staff table schema: staff_id (PK), name, dept_code, level
