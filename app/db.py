"""Postgres/SQLite persistence for project brief leads."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import database_url

STATUS_PENDING = "pending_payment"
STATUS_PAID = "paid"
STATUS_ABANDONED = "abandoned"

CONTACT_EMAIL = "email"
CONTACT_PHONE = "phone"

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    pass


class ProjectBrief(Base):
    __tablename__ = "project_briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    website: Mapped[str] = mapped_column(String(2048), nullable=False)
    contact_method: Mapped[str] = mapped_column(String(16), nullable=False)
    contact_value: Mapped[str] = mapped_column(String(512), nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_PENDING)
    stripe_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _session_factory() -> sessionmaker[Session]:
    global _engine, _SessionLocal
    if _SessionLocal is None:
        url = database_url()
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
            pool_kwargs: dict = {}
            if url.endswith(":memory:") or url.rstrip("/").endswith(":memory:"):
                pool_kwargs["poolclass"] = StaticPool
            _engine = create_engine(url, connect_args=connect_args, **pool_kwargs)
        else:
            _engine = create_engine(url)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _SessionLocal


def init_db() -> None:
    _session_factory()
    Base.metadata.create_all(_engine)


def reset_db_for_tests(url: str) -> None:
    """Point the module at a fresh in-memory database (tests only)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    import os

    os.environ["DATABASE_URL"] = url
    init_db()


def create_brief(
    *,
    website: str,
    contact_method: str,
    contact_value: str,
    brief: str,
) -> ProjectBrief:
    factory = _session_factory()
    with factory() as session:
        row = ProjectBrief(
            website=website,
            contact_method=contact_method,
            contact_value=contact_value,
            brief=brief,
            status=STATUS_PENDING,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def get_brief(brief_id: int) -> ProjectBrief | None:
    factory = _session_factory()
    with factory() as session:
        return session.get(ProjectBrief, brief_id)


def set_stripe_session(brief_id: int, session_id: str) -> ProjectBrief | None:
    factory = _session_factory()
    with factory() as session:
        row = session.get(ProjectBrief, brief_id)
        if row is None:
            return None
        row.stripe_session_id = session_id
        session.commit()
        session.refresh(row)
        return row


def mark_paid(
    brief_id: int,
    *,
    stripe_session_id: str,
    stripe_payment_intent_id: str | None,
) -> ProjectBrief | None:
    factory = _session_factory()
    with factory() as session:
        row = session.get(ProjectBrief, brief_id)
        if row is None:
            return None
        if row.status == STATUS_PAID:
            return row
        row.status = STATUS_PAID
        row.stripe_session_id = stripe_session_id
        row.stripe_payment_intent_id = stripe_payment_intent_id
        row.paid_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(row)
        return row


def get_brief_by_session_id(session_id: str) -> ProjectBrief | None:
    factory = _session_factory()
    with factory() as session:
        stmt = select(ProjectBrief).where(ProjectBrief.stripe_session_id == session_id)
        return session.scalars(stmt).first()
