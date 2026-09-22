"""
SQLAlchemy models + session for the AI email-coordination schema
===============================================================

Mirrors the production PostgreSQL DDL (Cloud SQL / GCP):
    oem_companies      OEM-<uuid>
    whitelist_senders  WLS-<uuid>
    campaigns          CMP-<uuid>
    campaign_suppliers SUP-<uuid>
    email_conversations CONV-<uuid>

The tables, enum types and extensions already exist in every environment
(dev + prod), created by the DDL script. This module contains **no** table /
type creation logic — only the connection engine, session helpers and the ORM
mappings used to query them. Every default (prefixed id, timestamps, booleans,
json) is declared as a **server_default**, so the database generates it, not
Python.

Usage
-----
    from db.dbmodel import session_scope, Campaign

    with session_scope() as db:
        db.add(Campaign(oem_company_id="OEM-...", title="Q3 sourcing",
                        org_email="buyer@oem.com", summary={"scope": "..."}))

Connection config is resolved through config.py. In production all DB_* values
live in ONE Secret Manager secret, COORDINATOR_DB_CONNECTION, holding a JSON
object; config.get("DB_HOST") etc. read fields out of it (an individual env var
still overrides a field). Keys used here:
    DB_HOST  DB_PORT  DB_NAME  DB_USER  DB_PASSWORD   [DB_SSLMODE]
    DB_POOL_SIZE  DB_MAX_OVERFLOW  DB_POOL_RECYCLE  DB_POOL_TIMEOUT  DB_ECHO
    or a single DATABASE_URL that overrides the discrete parts.
"""
from __future__ import annotations

import enum
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    URL,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from config import config  # every value: env var -> GCP Secret Manager

# --------------------------------------------------------------------------- #
# Engine / Session
# --------------------------------------------------------------------------- #
def _database_url() -> URL | str:
    explicit = config.get("DATABASE_URL")
    if explicit:
        return explicit

    query: dict[str, str] = {}
    sslmode = config.get("DB_SSLMODE")
    if sslmode:
        query["sslmode"] = sslmode

    return URL.create(
        drivername="postgresql+psycopg",
        username=config.get("DB_USER", "postgres"),
        password=config.get("DB_PASSWORD"),
        # Cloud SQL unix socket:  DB_HOST=/cloudsql/<project>:<region>:<instance>
        host=config.get("DB_HOST", "127.0.0.1"),
        port=config.get_int("DB_PORT"),
        database=config.get("DB_NAME", "postgres"),
        query=query,
    )


engine = create_engine(
    _database_url(),
    pool_pre_ping=True,                                   # drop dead connections
    pool_size=config.get_int("DB_POOL_SIZE", 5),
    max_overflow=config.get_int("DB_MAX_OVERFLOW", 10),
    pool_recycle=config.get_int("DB_POOL_RECYCLE", 1800),
    pool_timeout=config.get_int("DB_POOL_TIMEOUT", 30),
    echo=config.get_bool("DB_ECHO", False),
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error, always close."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session():
    """FastAPI/Flask-style dependency: `db = next(get_session())` or `yield`-inject."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Enums  (types are created by the DDL; create_type=False so ORM won't redefine)
# --------------------------------------------------------------------------- #
class CampaignSupplierStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"


class ConversationDirection(str, enum.Enum):
    INCOMING = "INCOMING"
    OUTGOING = "OUTGOING"


_supplier_status_enum = ENUM(
    CampaignSupplierStatus,
    name="campaign_supplier_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)
_direction_enum = ENUM(
    ConversationDirection,
    name="conversation_direction",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)


# --------------------------------------------------------------------------- #
# Declarative base + shared column types
# --------------------------------------------------------------------------- #
class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


def _prefixed_id(prefix: str) -> Mapped[str]:
    """TEXT primary key with server-side default  '<PREFIX>-' || gen_random_uuid()."""
    return mapped_column(
        Text,
        primary_key=True,
        server_default=text(f"('{prefix}-' || gen_random_uuid()::text)"),
    )


# --------------------------------------------------------------------------- #
# Table 1: oem_companies
# --------------------------------------------------------------------------- #
class OemCompany(Base):
    __tablename__ = "oem_companies"

    id: Mapped[str] = _prefixed_id("OEM")
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    subscribed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    started_at: Mapped[object] = mapped_column(TS, nullable=False)
    ended_at: Mapped[object] = mapped_column(TS, nullable=False)
    api_use_key: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[object] = mapped_column(
        TS, nullable=False, server_default=func.now()
    )

    whitelist_senders: Mapped[list["WhitelistSender"]] = relationship(
        back_populates="oem_company", passive_deletes=True
    )
    campaigns: Mapped[list["Campaign"]] = relationship(
        back_populates="oem_company", passive_deletes=True
    )


# --------------------------------------------------------------------------- #
# Table 2: whitelist_senders
# --------------------------------------------------------------------------- #
class WhitelistSender(Base):
    __tablename__ = "whitelist_senders"
    __table_args__ = (
        Index("idx_whitelist_oem_company", "oem_company_id"),
    )

    id: Mapped[str] = _prefixed_id("WLS")
    oem_company_id: Mapped[Optional[str]] = mapped_column(
        Text, ForeignKey("oem_companies.id", ondelete="SET NULL"), nullable=True
    )
    oem_user_name: Mapped[str] = mapped_column(Text, nullable=False)
    oem_user_email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    oem_user_phone_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[Optional[bool]] = mapped_column(
        Boolean, server_default=text("true")
    )
    created_at: Mapped[object] = mapped_column(TS, server_default=func.now())

    oem_company: Mapped[Optional["OemCompany"]] = relationship(
        back_populates="whitelist_senders"
    )


# --------------------------------------------------------------------------- #
# Table 3: campaigns
# --------------------------------------------------------------------------- #
class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        Index("idx_campaigns_oem_active", "oem_company_id", "is_active"),
    )

    id: Mapped[str] = _prefixed_id("CMP")
    oem_company_id: Mapped[str] = mapped_column(
        Text, ForeignKey("oem_companies.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    org_email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    addl_email: Mapped[Optional[str]] = mapped_column(CITEXT, nullable=True)
    is_active: Mapped[Optional[bool]] = mapped_column(
        Boolean, server_default=text("true")
    )
    total_suppliers: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    started_at: Mapped[object] = mapped_column(
        TS, nullable=False, server_default=func.now()
    )
    ended_at: Mapped[Optional[object]] = mapped_column(TS, nullable=True)
    gcs_raw_payload_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(
        TS, nullable=False, server_default=func.now()
    )

    oem_company: Mapped["OemCompany"] = relationship(back_populates="campaigns")
    suppliers: Mapped[list["CampaignSupplier"]] = relationship(
        back_populates="campaign", passive_deletes=True
    )
    conversations: Mapped[list["EmailConversation"]] = relationship(
        back_populates="campaign", passive_deletes=True
    )


# --------------------------------------------------------------------------- #
# Table 4: campaign_suppliers
# --------------------------------------------------------------------------- #
class CampaignSupplier(Base):
    __tablename__ = "campaign_suppliers"
    __table_args__ = (
        UniqueConstraint("campaign_id", "user_email", name="uq_campaign_user_email"),
        Index("idx_supplier_campaign_active", "campaign_id", "is_active"),
        Index("idx_supplier_status_ooo", "status", "ooo_till"),
        Index("idx_supplier_is_active", "is_active"),
        Index("idx_supplier_ooo_till", "ooo_till"),
    )

    id: Mapped[str] = _prefixed_id("SUP")
    campaign_id: Mapped[str] = mapped_column(
        Text, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    user_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    user_phone_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    company_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    company_domain: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    requirements_fulfillment: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    status: Mapped[CampaignSupplierStatus] = mapped_column(
        _supplier_status_enum, nullable=False, server_default=text("'pending'")
    )
    assigned_at: Mapped[object] = mapped_column(
        TS, nullable=False, server_default=func.now()
    )
    completed_at: Mapped[Optional[object]] = mapped_column(TS, nullable=True)
    reminder_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_reminder_sent_at: Mapped[Optional[object]] = mapped_column(TS, nullable=True)

    # Out-of-office & alternate routing
    alternate_user_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    alternate_email: Mapped[Optional[str]] = mapped_column(CITEXT, nullable=True)
    alternate_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    out_of_office: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    ooo_till: Mapped[Optional[object]] = mapped_column(TS, nullable=True)

    campaign: Mapped["Campaign"] = relationship(back_populates="suppliers")
    conversations: Mapped[list["EmailConversation"]] = relationship(
        back_populates="supplier", passive_deletes=True
    )


# --------------------------------------------------------------------------- #
# Table 5: email_conversations
# --------------------------------------------------------------------------- #
class EmailConversation(Base):
    __tablename__ = "email_conversations"
    __table_args__ = (
        Index("idx_conversation_campaign", "campaign_id"),
        Index("idx_conversation_supplier", "campaign_supplier_id"),
        Index("idx_conversation_thread", "thread_id"),
        Index("idx_conversation_sender", "sender_email"),
    )

    id: Mapped[str] = _prefixed_id("CONV")
    campaign_id: Mapped[Optional[str]] = mapped_column(
        Text, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    campaign_supplier_id: Mapped[Optional[str]] = mapped_column(
        Text, ForeignKey("campaign_suppliers.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    in_reply_to: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sender_email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    recipients: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("""'{"to": [], "cc": [], "bcc": []}'::jsonb"""),
    )
    direction: Mapped[ConversationDirection] = mapped_column(
        _direction_enum, nullable=False
    )
    subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_urls: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    processing_status: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'PROCESSED'")
    )
    created_at: Mapped[object] = mapped_column(
        TS, nullable=False, server_default=func.now()
    )

    campaign: Mapped[Optional["Campaign"]] = relationship(back_populates="conversations")
    supplier: Mapped[Optional["CampaignSupplier"]] = relationship(
        back_populates="conversations"
    )


if __name__ == "__main__":
    # Quick connectivity check:  python -m db.dbmodel
    with engine.connect() as conn:
        print("connected:", conn.execute(text("select version()")).scalar())
