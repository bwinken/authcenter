"""SQLAlchemy models for both databases."""

from datetime import datetime

from sqlalchemy import String, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# --- SQLite Models (Auth Local DB) ---

class SQLiteBase(DeclarativeBase):
    pass


class UserAccount(SQLiteBase):
    __tablename__ = "user_accounts"

    employee_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


# --- MySQL model (IT Master DB - read only, reflected) ---
# We use raw SQL queries for the MySQL staff table to avoid
# needing a separate DeclarativeBase bound to the MySQL engine.
# The staff table schema: staff_id (PK), name, dept_code, level, ext
# staff_id maps to "employee_name" in our system (e.g. kane.beh)
