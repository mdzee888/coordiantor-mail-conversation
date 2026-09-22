"""
templates.py - plain-text email bodies the pipeline sends itself (no LLM).

- subscription_inactive_email: sent back to a whitelisted sender whose OEM
  company has no valid subscription.
- supplier_fulfilled_notification_email: sent to the campaign creator once a
  supplier's reply is confirmed to fulfil that campaign's requirements.
- duplicate_campaign_email: sent back to a whitelisted sender whose request
  matches an already-active campaign, instead of creating a second one.
- ooo_redirect_email: forwards a campaign request to a supplier's named
  alternate contact while the supplier is out of office.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _first_name(full_name: Optional[str]) -> str:
    if not full_name:
        return "there"
    return full_name.strip().split()[0]


def subscription_inactive_email(
    *,
    recipient_name: Optional[str] = None,
    company_name: Optional[str] = None,
    ended_at: Optional[datetime] = None,
) -> tuple[str, str]:
    """Return (subject, body) for the 'no valid subscription' reply."""
    name = _first_name(recipient_name)
    org = company_name or "your organisation"

    if ended_at is not None:
        when = ended_at.astimezone(timezone.utc).date().isoformat()
        status_line = (
            f"the coordination subscription for {org} ended on {when} and is no "
            f"longer active"
        )
    else:
        status_line = f"there is no active coordination subscription for {org}"

    subject = "Your coordination request could not be processed - subscription inactive"
    body = (
        f"Hi {name},\n\n"
        f"Thanks for your email. We could not process this request because "
        f"{status_line}.\n\n"
        f"Please renew the subscription and resend your request, or contact your "
        f"account manager for help reactivating it.\n\n"
        f"Once the subscription is active again, forward this email to us and we "
        f"will take it from there.\n\n"
        f"Thanks,\n"
        f"Coordinator\n"
    )
    return subject, body


def supplier_fulfilled_notification_email(
    *,
    campaign_title: str,
    supplier_name: Optional[str],
    supplier_email: str,
    supplier_company: Optional[str] = None,
    response_summary: Optional[Dict[str, Any]] = None,
) -> tuple[str, str]:
    """Return (subject, body) telling the campaign creator a supplier is fully done."""
    who = supplier_name or supplier_email
    org = f" ({supplier_company})" if supplier_company else ""

    subject = f"Supplier requirement fulfilled: {campaign_title} - {who}"
    lines = [
        "Hi,",
        "",
        f"{who}{org} <{supplier_email}> has provided everything requested for the "
        f"campaign \"{campaign_title}\".",
        "",
    ]
    if response_summary:
        lines.append("Details provided:")
        lines += [f"- {k}: {v}" for k, v in response_summary.items()]
        lines.append("")
    lines += ["Thanks,", "Coordinator", ""]
    return subject, "\n".join(lines)


def duplicate_campaign_email(
    *,
    recipient_name: Optional[str] = None,
    campaign_title: str,
    campaign_id: str,
) -> tuple[str, str]:
    """Return (subject, body) telling the OEM sender this request is already active."""
    name = _first_name(recipient_name)
    subject = f"Already in progress: {campaign_title}"
    body = (
        f"Hi {name},\n\n"
        f"Thanks for your email. A campaign for \"{campaign_title}\" is already active "
        f"(reference {campaign_id}), so we have not started a new one or re-contacted "
        f"the suppliers.\n\n"
        f"If this is a different request, please resend with a different subject or a "
        f"more specific objective so we can tell them apart. If you would like a status "
        f"update on the existing one, just ask.\n\n"
        f"Thanks,\n"
        f"Coordinator\n"
    )
    return subject, body


def ooo_redirect_email(
    *,
    alternate_name: Optional[str],
    original_supplier_name: Optional[str],
    original_supplier_email: str,
    campaign_title: str,
    original_subject: Optional[str],
    original_body: str,
) -> tuple[str, str]:
    """Return (subject, body) forwarding a campaign request to a named alternate
    contact while the original supplier is out of office."""
    name = _first_name(alternate_name)
    who = original_supplier_name or original_supplier_email

    subject = f"On behalf of {who}: {original_subject or campaign_title}"
    body = (
        f"Hi {name},\n\n"
        f"{who} <{original_supplier_email}> is currently out of office and named "
        f"you as the point of contact in the meantime, so we're forwarding this "
        f"request from the \"{campaign_title}\" campaign to you.\n\n"
        f"----- Original message -----\n"
        f"{original_body}\n"
    )
    return subject, body
