"""
json_schema.py - the JSON schema (Pydantic models) for every agent.

campaign_create:
* Extraction : exactly what the LLM returns - no computed fields.
* Campaign   : the final, DB-ready object after finalize() adds timestamps,
               counts, and the resolved deadline date.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# =========================================================================== #
# campaign_create
# =========================================================================== #


class Supplier(BaseModel):
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    user_phone_number: Optional[str] = None
    company_name: Optional[str] = None
    company_domain: Optional[str] = None


class Deadline(BaseModel):
    mentioned_in_email: Optional[str] = None       # exact phrase from the email
    duration_days: Optional[int] = None            # N if "within N days"
    explicit_date: Optional[str] = None            # YYYY-MM-DD, only if named
    calculated_end_date: Optional[str] = None      # filled by finalize()


class Summary(BaseModel):
    """Requirement-related information ONLY - no supplier names / contacts here."""

    objective: Optional[str] = None
    instructions: List[str] = Field(default_factory=list)
    important_information: List[str] = Field(default_factory=list)
    cc_emails: List[str] = Field(default_factory=list)
    bcc_emails: List[str] = Field(default_factory=list)
    additional_information: Optional[str] = None
    deadline: Deadline = Field(default_factory=Deadline)


class Extraction(BaseModel):
    """Exactly what the LLM returns - no computed fields."""

    title: str
    org_email: Optional[str] = None
    addl_email: List[str] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    supplier_list: List[Supplier] = Field(default_factory=list)


class Campaign(BaseModel):
    """Final object - ready to insert / update (timestamps are UTC)."""

    title: str
    org_email: Optional[str] = None
    addl_email: List[str] = Field(default_factory=list)
    summary: Summary
    supplier_list: List[Supplier] = Field(default_factory=list)
    is_active: bool = True
    total_suppliers: int = 0
    started_at: str                       # UTC ISO 8601, e.g. "2026-08-29T00:11:00Z"
    ended_at: Optional[str] = None        # UTC ISO 8601


# =========================================================================== #
# email_purpose_check
# =========================================================================== #


class PurposeCheck(BaseModel):
    """What an inbound coordinator email is asking for."""

    purpose: Literal[
        "campaign_create",   # start a new supplier-outreach campaign
        "task",              # an actionable request -> also handled as a campaign
        "supplier_reply",    # a supplier answering an existing campaign
        "question",          # a question / clarification for the coordinator
        "other",             # anything else
    ]
    reason: Optional[str] = None          # one short sentence of justification


# =========================================================================== #
# campaign_mail_creator
# =========================================================================== #


class MailDraft(BaseModel):
    """A ready-to-send email built from a campaign summary."""

    subject: str
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    mail_content: str                     # complete, formatted plain-text body


# =========================================================================== #
# supplier_reply_check
# =========================================================================== #


class SupplierReplyCheck(BaseModel):
    """Whether a supplier's latest reply, in context of the full thread, satisfies
    the campaign's requirements - plus the reply to send back to them."""

    fulfilled: bool
    missing_items: List[str] = Field(default_factory=list)
    response_summary: Dict[str, Any] = Field(default_factory=dict)
    reply_subject: str
    reply_body: str                       # complete, formatted plain-text body


# =========================================================================== #
# supplier_ooo_check
# =========================================================================== #


class OooCheck(BaseModel):
    """Whether one inbound email is an automated out-of-office / auto-reply,
    and - only if it is - the return timing and alternate contact it names."""

    is_out_of_office: bool
    duration_days: Optional[int] = None    # N if "back in N days" / "away for N days"
    return_date: Optional[str] = None      # YYYY-MM-DD, only if an explicit date is named
    alternate_name: Optional[str] = None
    alternate_email: Optional[str] = None
    alternate_phone: Optional[str] = None
