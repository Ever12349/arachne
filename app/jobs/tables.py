"""SQLAlchemy tables for persisted jobs and items."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.db import Base


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_client_id", "client_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    items: Mapped[list[JobItemRow]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobItemRow.index",
        lazy="selectin",
    )


class JobItemRow(Base):
    __tablename__ = "job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "index", name="uq_job_items_job_id_index"),
        Index("ix_job_items_requested_url", "requested_url"),
        Index("ix_job_items_result_url", "result_url"),
        Index("ix_job_items_job_id", "job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    result_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    cookies: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    max_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ua_strategy: Mapped[str] = mapped_column(String(16), nullable=False)
    render: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    site_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    job: Mapped[JobRow] = relationship(back_populates="items")
