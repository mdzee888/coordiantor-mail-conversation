"""Database layer: SQLAlchemy models + session for the email coordination schema."""
from db.dbmodel import (
    Base,
    engine,
    SessionLocal,
    session_scope,
    get_session,
    OemCompany,
    WhitelistSender,
    Campaign,
    CampaignSupplier,
    EmailConversation,
    CampaignSupplierStatus,
    ConversationDirection,
)

__all__ = [
    "Base",
    "engine",
    "SessionLocal",
    "session_scope",
    "get_session",
    "OemCompany",
    "WhitelistSender",
    "Campaign",
    "CampaignSupplier",
    "EmailConversation",
    "CampaignSupplierStatus",
    "ConversationDirection",
]
