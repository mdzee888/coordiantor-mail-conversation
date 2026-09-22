"""
pipeline.py - what happens when a mail lands in the coordinator inbox.

Called by every provider's handler after it fetches + marks-read a message:

    from pipeline import process
    process(normalized_msg, provider, coordinator_address)

Flow
----
1. sender must be in `whitelist_senders` (active)                     -> else 1b
   1b. not whitelisted -> sender must instead match an existing, active
       row in `campaign_suppliers` (a supplier replying to a campaign) ->
       else ignore. If matched, see "Supplier-reply flow" below and stop.
2. that row's `oem_company_id` -> `oem_companies`
     - if `ended_at` is in the past, flip `subscribed` to false
     - if not subscribed / outside started_at..ended_at:
         reply "subscription inactive" (templates.py) and stop
3. subscription valid -> classify the mail (llm-agents/email_purpose_check.py)
     - purpose not in {campaign_create, task} -> log the inbound and stop
4. run llm-agents/campaign.py `run()`  -> campaign dict
   4b. same oem_company_id already has an ACTIVE campaign with the same title or
       summary.objective -> reply "already in progress" (templates.py) and stop;
       no new `campaigns`/`campaign_suppliers` rows, no suppliers re-contacted
     - insert 1 row in `campaigns`
     - insert 1 row per supplier in `campaign_suppliers`
     - save the inbound mail in `email_conversations` (direction INCOMING)
5. take the stored campaign `summary` -> llm-agents/campaign_mail_creator.py `run()`
     - send the resulting mail to every supplier, one by one
     - save every outbound mail in `email_conversations` (direction OUTGOING)

Supplier-reply flow (sender found in `campaign_suppliers`, not in `whitelist_senders`;
matches either the supplier's own `user_email` or their `alternate_email`)
---------------------------------------------------------------------------------------
- load that supplier's campaign (`campaigns.summary`) and its full prior
  `email_conversations` thread with this supplier
- llm-agents/supplier_ooo_check.py `run()` on just the new mail, first:
     - IS an out-of-office auto-reply -> record out_of_office / ooo_till /
       alternate_user_name / alternate_email / alternate_phone on
       `campaign_suppliers`, forward the last outbound mail to the alternate
       contact if one is known (templates.py), log it, and stop - no
       fulfillment check runs on an auto-reply
     - NOT an auto-reply -> clear out_of_office (a real person is responding,
       from the supplier or their alternate) and continue:
- llm-agents/supplier_reply_check.py `run()` on requirements + thread + new mail
     - reply to the supplier (fulfilled confirmation, or the still-missing items)
     - update the `campaign_suppliers` row: response_summary,
       requirements_fulfillment, status (in_progress / completed), completed_at
     - if fulfilled: also email the campaign creator (`campaigns.org_email`)
       with the supplier's response_summary attached
     - save every mail (in + out) in `email_conversations`
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select

import mailer
from templates import (
    subscription_inactive_email,
    supplier_fulfilled_notification_email,
    duplicate_campaign_email,
    ooo_redirect_email,
)
from db import (
    session_scope,
    OemCompany,
    WhitelistSender,
    Campaign,
    CampaignSupplier,
    CampaignSupplierStatus,
    EmailConversation,
    ConversationDirection,
)

# llm-agents/ has a hyphen -> not importable as a package; add it to sys.path.
_LLM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm-agents")
if _LLM_DIR not in sys.path:
    sys.path.insert(0, _LLM_DIR)

CAMPAIGN_PURPOSES = ("campaign_create", "task")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _addr(value: str) -> str:
    from email.utils import parseaddr

    return parseaddr(value or "")[1].strip().lower()


def _sender(msg: dict) -> str:
    frm = msg.get("from") or []
    return _addr(frm[0]) if frm else ""


def _dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ooo_till(duration_days, return_date, now) -> "datetime | None":
    """OOO result fields -> the datetime the supplier is expected back, if derivable."""
    if duration_days is not None:
        return now + timedelta(days=duration_days)
    if return_date:
        try:
            d = datetime.strptime(str(return_date), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return d.replace(hour=now.hour, minute=now.minute, second=now.second)
        except ValueError:
            return None
    return None


def _last_outgoing(history):
    """Most recent OUTGOING EmailConversation in an oldest-first list, if any."""
    for m in reversed(history):
        if m.direction == ConversationDirection.OUTGOING:
            return m
    return None


def _email_text(msg: dict) -> str:
    return "\n".join([
        f"From: {', '.join(msg.get('from') or [])}",
        f"To: {', '.join(msg.get('to') or [])}",
        f"Cc: {', '.join(msg.get('cc') or [])}",
        f"Bcc: {', '.join(msg.get('bcc') or [])}",
        f"Subject: {msg.get('subject') or ''}",
        "",
        (msg.get("text") or "").strip() or (msg.get("html") or "").strip(),
    ])


def _new_mid() -> str:
    return f"<{uuid.uuid4()}@coordinator.local>"


def _first_name(full_name) -> str:
    return full_name.strip().split()[0] if full_name and full_name.strip() else "there"


def _signature(name, email, phone=None) -> str:
    lines = [name or "Coordinator"]
    if email:
        lines.append(email)
    if phone:
        lines.append(phone)
    return "\n".join(lines)


#: matches {SUPPLIER_NAME} or {{SUPPLIER_NAME}} - models don't always double the braces
#: as instructed, so tolerate either rather than leaking a raw token into a live email.
_TOKEN_RE = re.compile(r"\{{1,2}\s*(SUPPLIER_NAME|SENDER_SIGNATURE)\s*\}{1,2}")


def _personalize(mail_content: str, supplier_name, signature: str) -> str:
    values = {"SUPPLIER_NAME": _first_name(supplier_name), "SENDER_SIGNATURE": signature}
    return _TOKEN_RE.sub(lambda m: values[m.group(1)], mail_content)


def _supplier_reply_input(campaign_summary: dict, history, new_msg_text: str) -> str:
    """campaign requirements + prior thread + new mail -> one text blob for the LLM."""
    parts = [
        "CAMPAIGN REQUIREMENTS",
        "======================",
        json.dumps(campaign_summary or {}, indent=2, default=str),
        "",
        "CONVERSATION HISTORY (oldest to newest, excluding this new message)",
        "======================",
    ]
    for m in history:
        when = m.created_at.isoformat() if m.created_at else ""
        parts.append(
            f"[{m.direction.value}] {when} from {m.sender_email}\n"
            f"Subject: {m.subject or ''}\n{(m.body_text or '').strip()}\n"
        )
    parts += [
        "NEW MESSAGE FROM SUPPLIER (this is what you are evaluating)",
        "======================",
        new_msg_text,
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# llm-agents runners (imported lazily - they pull in litellm)
# --------------------------------------------------------------------------- #
def _run_purpose(text: str) -> dict:
    import email_purpose_check

    return email_purpose_check.run(text)


def _run_campaign(text: str) -> dict:
    import campaign

    return campaign.run(text)


def _run_mail(summary: dict) -> dict:
    import campaign_mail_creator

    return campaign_mail_creator.run(summary)


def _run_supplier_reply(text: str) -> dict:
    import supplier_reply_check

    return supplier_reply_check.run(text)


def _run_ooo_check(text: str) -> dict:
    import supplier_ooo_check

    return supplier_ooo_check.run(text)


# --------------------------------------------------------------------------- #
# send wrappers - never raise
# --------------------------------------------------------------------------- #
def _safe_send(provider: str, to: str, mail: dict) -> dict:
    try:
        r = mailer.send(provider, to=to, subject=mail["subject"],
                        text=mail["mail_content"],
                        cc=mail.get("cc"), bcc=mail.get("bcc")) or {}
        return {"ok": True, **r}
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] send to {to} failed: {exc}")
        return {"ok": False}


def _safe_reply(provider: str, msg: dict, subject: str, body: str,
                cc=None, bcc=None) -> dict:
    try:
        r = mailer.reply(provider, msg, text=body, subject=subject, cc=cc, bcc=bcc) or {}
        return {"ok": True, **r}
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] reply failed: {exc}")
        return {"ok": False}


# --------------------------------------------------------------------------- #
# email_conversations rows
# --------------------------------------------------------------------------- #
def _log_incoming(db, msg, sender, campaign_id, supplier_id, body_text, status):
    db.add(EmailConversation(
        campaign_id=campaign_id,
        campaign_supplier_id=supplier_id,
        message_id=msg.get("message_id") or _new_mid(),
        thread_id=msg.get("thread_id") or msg.get("message_id") or str(uuid.uuid4()),
        in_reply_to=msg.get("in_reply_to"),
        sender_email=sender,
        recipients={
            "to": msg.get("to") or [],
            "cc": msg.get("cc") or [],
            "bcc": msg.get("bcc") or [],
        },
        direction=ConversationDirection.INCOMING,
        subject=msg.get("subject"),
        body_text=body_text,
        attachment_urls=[],
        processing_status=status,
    ))


def _log_outgoing(db, campaign_id, supplier_id, from_addr, to_addrs, subject,
                  body, out, in_reply_to=None, cc=None, bcc=None):
    db.add(EmailConversation(
        campaign_id=campaign_id,
        campaign_supplier_id=supplier_id,
        message_id=out.get("message_id") or _new_mid(),
        thread_id=out.get("thread_id") or campaign_id or str(uuid.uuid4()),
        in_reply_to=in_reply_to,
        sender_email=from_addr,
        recipients={"to": to_addrs, "cc": cc or [], "bcc": bcc or []},
        direction=ConversationDirection.OUTGOING,
        subject=subject,
        body_text=body,
        attachment_urls=[],
        processing_status="SENT" if out.get("ok") else "SEND_FAILED",
    ))


# --------------------------------------------------------------------------- #
# db gates
# --------------------------------------------------------------------------- #
def _already_processed(db, message_id) -> bool:
    if not message_id:
        return False
    return db.execute(
        select(EmailConversation.id).where(
            EmailConversation.message_id == message_id,
            EmailConversation.direction == ConversationDirection.INCOMING,
        )
    ).first() is not None


def _whitelist_row(db, sender):
    ws = db.execute(
        select(WhitelistSender).where(
            func.lower(WhitelistSender.oem_user_email) == sender
        )
    ).scalars().first()
    if ws is not None and ws.is_active is False:
        return None
    return ws


def _subscription_valid(company, now) -> bool:
    if company is None or not company.subscribed:
        return False
    if company.started_at is not None and company.started_at > now:
        return False
    if company.ended_at is not None and company.ended_at < now:
        return False
    return True


def _org_creator(db, org_email):
    """(name, email, phone) of the whitelisted sender behind a campaign's org_email, if any."""
    if not org_email:
        return None, None, None
    ws = _whitelist_row(db, org_email.strip().lower())
    if ws is None:
        return None, org_email, None
    return ws.oem_user_name, org_email, ws.oem_user_phone_number


def _normalize(text) -> str:
    return " ".join((text or "").split()).strip().lower()


#: strips one or more leading Re:/Fwd:/Fw: prefixes off a subject line
_REPLY_PREFIX_RE = re.compile(r"^(re|fwd?|fw)\s*:\s*", re.IGNORECASE)


def _normalize_subject(subject) -> str:
    s = _normalize(subject)
    while True:
        stripped = _REPLY_PREFIX_RE.sub("", s)
        if stripped == s:
            return s
        s = stripped


def _find_active_duplicate(db, oem_company_id, title, objective):
    """An existing active campaign for this OEM company with the same title or
    objective, if any - so the same request re-sent doesn't spawn a second campaign
    and re-contact every supplier again."""
    if not oem_company_id:
        return None
    norm_title = _normalize(title)
    norm_objective = _normalize(objective)
    candidates = db.execute(
        select(Campaign).where(
            Campaign.oem_company_id == oem_company_id,
            Campaign.is_active.is_(True),
        )
    ).scalars().all()
    for c in candidates:
        if norm_title and _normalize(c.title) == norm_title:
            return c
        if norm_objective and _normalize((c.summary or {}).get("objective")) == norm_objective:
            return c
    return None


def _supplier_candidates(db, sender):
    """Every active `campaign_suppliers` row for this sender, most recently assigned
    first - matches either the supplier's own email or their out-of-office alternate."""
    return db.execute(
        select(CampaignSupplier)
        .where(or_(func.lower(CampaignSupplier.user_email) == sender,
                   func.lower(CampaignSupplier.alternate_email) == sender),
              CampaignSupplier.is_active.is_(True))
        .order_by(CampaignSupplier.assigned_at.desc())
    ).scalars().all()


def _supplier_row(db, sender, msg=None):
    """Which active `campaign_suppliers` row an inbound mail belongs to. The same
    email can be an active supplier in more than one campaign at once, so when it
    is, disambiguate in priority order:
      1. exact thread match - this mail's `thread_id` / `in_reply_to` lines up with
         an `email_conversations` row already logged against one of the candidates
      2. subject match - the mail's subject (Re:/Fwd: stripped) matches one
         candidate's campaign title
      3. fallback - most recently assigned candidate (the old behaviour)
    """
    candidates = _supplier_candidates(db, sender)
    if not candidates:
        return None
    if len(candidates) == 1:
        c = candidates[0]
        print(f"[pipeline] {sender}: single active campaign - supplier {c.id} "
              f"(campaign {c.campaign_id})")
        return c

    candidate_ids = [c.id for c in candidates]
    msg = msg or {}

    # 1. exact thread match
    thread_id = msg.get("thread_id")
    in_reply_to = msg.get("in_reply_to")
    if thread_id or in_reply_to:
        conds = [cond for cond in (
            EmailConversation.thread_id == thread_id if thread_id else None,
            EmailConversation.message_id == in_reply_to if in_reply_to else None,
        ) if cond is not None]
        hit_id = db.execute(
            select(EmailConversation.campaign_supplier_id)
            .where(EmailConversation.campaign_supplier_id.in_(candidate_ids), or_(*conds))
            .order_by(EmailConversation.created_at.desc())
        ).scalars().first()
        if hit_id:
            match = next(c for c in candidates if c.id == hit_id)
            print(f"[pipeline] {sender}: {len(candidates)} active campaigns - matched by "
                  f"thread -> supplier {match.id} (campaign {match.campaign_id})")
            return match

    # 2. subject match against each candidate's own campaign title
    subject_norm = _normalize_subject(msg.get("subject"))
    if subject_norm:
        for c in candidates:
            campaign = db.get(Campaign, c.campaign_id)
            if campaign and _normalize_subject(campaign.title) == subject_norm:
                print(f"[pipeline] {sender}: {len(candidates)} active campaigns - matched "
                      f"by subject -> supplier {c.id} (campaign {c.campaign_id})")
                return c

    # 3. fallback - most recently assigned
    match = candidates[0]
    print(f"[pipeline] {sender}: {len(candidates)} active campaigns, no thread/subject "
          f"match - falling back to most recent -> supplier {match.id} "
          f"(campaign {match.campaign_id})")
    return match


def _conversation_history(db, campaign_supplier_id):
    """Full prior thread with this supplier, oldest first."""
    return db.execute(
        select(EmailConversation)
        .where(EmailConversation.campaign_supplier_id == campaign_supplier_id)
        .order_by(EmailConversation.created_at.asc())
    ).scalars().all()


# --------------------------------------------------------------------------- #
# supplier-reply flow (sender in `campaign_suppliers`, not `whitelist_senders`)
# --------------------------------------------------------------------------- #
def _process_supplier_reply(msg, provider, coordinator_address, sender, supplier_id) -> None:
    """Check the new reply + full thread against the campaign's requirements, reply to
    the supplier, and - once fulfilled - notify the campaign creator."""
    message_id = msg.get("message_id")
    body_text = _email_text(msg)

    with session_scope() as db:
        supplier = db.get(CampaignSupplier, supplier_id)
        if supplier is None:
            print(f"[pipeline] supplier {supplier_id} vanished; ignoring")
            return
        campaign_id = supplier.campaign_id
        campaign_row = db.get(Campaign, campaign_id)
        campaign_summary = campaign_row.summary if campaign_row else {}
        campaign_title = campaign_row.title if campaign_row else "campaign"
        campaign_org_email = campaign_row.org_email if campaign_row else None
        supplier_name = supplier.user_name
        supplier_company = supplier.company_name
        supplier_user_email = supplier.user_email
        existing_summary = dict(supplier.response_summary or {})
        existing_alt_name = supplier.alternate_user_name
        existing_alt_email = supplier.alternate_email
        existing_alt_phone = supplier.alternate_phone
        history = _conversation_history(db, supplier_id)
        creator_name, creator_email, creator_phone = _org_creator(db, campaign_org_email)

    # out-of-office check - an auto-reply is not a real answer, handle it separately #
    ooo = _run_ooo_check(body_text)
    if not ooo.get("error") and ooo.get("is_out_of_office"):
        now = _now()
        till = _ooo_till(ooo.get("duration_days"), ooo.get("return_date"), now)
        alt_name = ooo.get("alternate_name") or existing_alt_name
        alt_email = (ooo.get("alternate_email") or existing_alt_email or "").strip().lower() or None
        alt_phone = ooo.get("alternate_phone") or existing_alt_phone

        with session_scope() as db:
            _log_incoming(db, msg, sender, campaign_id, supplier_id, body_text, "OUT_OF_OFFICE")
            supplier = db.get(CampaignSupplier, supplier_id)
            if supplier is not None:
                supplier.out_of_office = True
                supplier.ooo_till = till
                supplier.alternate_user_name = alt_name
                supplier.alternate_email = alt_email
                supplier.alternate_phone = alt_phone

        print(f"[pipeline] supplier {sender}: out of office"
              f"{f' till {till.date()}' if till else ''}"
              f"{f', alternate {alt_email}' if alt_email else ''}")

        if alt_email and alt_email != sender:
            last_out = _last_outgoing(history)
            fwd_subject, fwd_body = ooo_redirect_email(
                alternate_name=alt_name,
                original_supplier_name=supplier_name,
                original_supplier_email=supplier_user_email,
                campaign_title=campaign_title,
                original_subject=last_out.subject if last_out else None,
                original_body=(last_out.body_text if last_out
                              else (campaign_summary or {}).get("objective") or ""),
            )
            fwd_out = _safe_send(provider, alt_email,
                                 {"subject": fwd_subject, "mail_content": fwd_body})
            with session_scope() as db:
                _log_outgoing(db, campaign_id, supplier_id, coordinator_address,
                              [alt_email], fwd_subject, fwd_body, fwd_out)
            print(f"[pipeline] campaign {campaign_id}: forwarded to alternate {alt_email}")
        return

    thread_text = _supplier_reply_input(campaign_summary, history, body_text)
    result = _run_supplier_reply(thread_text)

    if result.get("error"):
        print(f"[pipeline] supplier_reply_check failed for {sender}: {result['error']}")
        with session_scope() as db:
            _log_incoming(db, msg, sender, campaign_id, supplier_id, body_text,
                          "SUPPLIER_CHECK_FAILED")
        return

    fulfilled = bool(result.get("fulfilled"))
    reply_subject = result.get("reply_subject") or f"Re: {msg.get('subject') or campaign_title}"
    signature = _signature(creator_name, creator_email, creator_phone)
    reply_body = _personalize(result.get("reply_body") or "", supplier_name, signature)
    merged_summary = {**existing_summary, **(result.get("response_summary") or {})}
    reply_cc = (campaign_summary or {}).get("cc_emails") or []
    reply_bcc = (campaign_summary or {}).get("bcc_emails") or []

    out = _safe_reply(provider, msg, reply_subject, reply_body, cc=reply_cc, bcc=reply_bcc)

    with session_scope() as db:
        _log_incoming(db, msg, sender, campaign_id, supplier_id, body_text, "PROCESSED")
        supplier = db.get(CampaignSupplier, supplier_id)
        if supplier is not None:
            supplier.response_summary = merged_summary
            supplier.requirements_fulfillment = {
                "fulfilled": fulfilled, "missing_items": result.get("missing_items") or [],
            }
            supplier.status = (CampaignSupplierStatus.completed if fulfilled
                               else CampaignSupplierStatus.in_progress)
            # a real reply just arrived (from the supplier or their alternate) - any
            # prior out-of-office period is over; future replies go to the supplier again
            supplier.out_of_office = False
            if fulfilled:
                supplier.completed_at = _now()
        _log_outgoing(db, campaign_id, supplier_id, coordinator_address, [sender],
                      reply_subject, reply_body, out, in_reply_to=message_id,
                      cc=reply_cc, bcc=reply_bcc)

    print(f"[pipeline] supplier {sender}: fulfilled={fulfilled} (campaign {campaign_id})")

    if not fulfilled or not campaign_org_email:
        return

    # notify the campaign creator - the supplier has met every requirement
    notif_subject, notif_body = supplier_fulfilled_notification_email(
        campaign_title=campaign_title,
        supplier_name=supplier_name,
        supplier_email=sender,
        supplier_company=supplier_company,
        response_summary=merged_summary,
    )
    notif_out = _safe_send(provider, campaign_org_email,
                           {"subject": notif_subject, "mail_content": notif_body})
    with session_scope() as db:
        _log_outgoing(db, campaign_id, supplier_id, coordinator_address,
                      [campaign_org_email], notif_subject, notif_body, notif_out)
    print(f"[pipeline] campaign {campaign_id}: notified creator {campaign_org_email}")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def process(msg: dict, provider: str, coordinator_address: str) -> None:
    sender = _sender(msg)
    if not sender:
        print("[pipeline] no sender address; skipping")
        return
    message_id = msg.get("message_id")

    # 1. whitelist + subscription gate ----------------------------------- #
    with session_scope() as db:
        if _already_processed(db, message_id):
            print(f"[pipeline] {message_id} already processed; skipping")
            return

        ws = _whitelist_row(db, sender)
        supplier_id = None
        if ws is None:
            supplier = _supplier_row(db, sender, msg)
            if supplier is None:
                print(f"[pipeline] {sender} not in whitelist_senders or "
                      f"campaign_suppliers; ignoring")
                return
            supplier_id = supplier.id
        else:
            company = db.get(OemCompany, ws.oem_company_id) if ws.oem_company_id else None
            now = _now()
            if (company is not None and company.ended_at is not None
                    and company.ended_at < now and company.subscribed):
                company.subscribed = False
                print(f"[pipeline] company {company.id} expired -> subscribed=false")

            valid = _subscription_valid(company, now)
            oem_company_id = ws.oem_company_id
            oem_user_name = ws.oem_user_name
            oem_user_phone = ws.oem_user_phone_number
            company_name = getattr(company, "company_name", None)
            company_ended = getattr(company, "ended_at", None)

    # 1b. no whitelist match, but sender is a known campaign supplier -------- #
    if supplier_id is not None:
        _process_supplier_reply(msg, provider, coordinator_address, sender, supplier_id)
        return

    # 2. no subscription -> reply and stop ------------------------------- #
    if not valid:
        subject, body = subscription_inactive_email(
            recipient_name=oem_user_name,
            company_name=company_name,
            ended_at=company_ended,
        )
        out = _safe_reply(provider, msg, subject, body)
        with session_scope() as db:
            _log_incoming(db, msg, sender, None, None,
                          _email_text(msg), "NO_SUBSCRIPTION")
            _log_outgoing(db, None, None, coordinator_address, [sender],
                          subject, body, out, in_reply_to=message_id)
        print(f"[pipeline] {sender}: subscription inactive - replied")
        return

    # 3. classify the mail --------------------------------------------- #
    body_text = _email_text(msg)
    purpose = _run_purpose(body_text)
    pval = purpose.get("purpose")
    print(f"[pipeline] purpose={pval} reason={purpose.get('reason')}")
    if pval not in CAMPAIGN_PURPOSES:
        with session_scope() as db:
            _log_incoming(db, msg, sender, None, None, body_text,
                          f"SKIPPED_{(pval or 'UNKNOWN').upper()}")
        return

    # 4. extract the campaign ---------------------------------------- #
    result = _run_campaign(body_text)
    if result.get("error") or not result.get("campaign"):
        print(f"[pipeline] campaign extraction failed: {result.get('error')}")
        with session_scope() as db:
            _log_incoming(db, msg, sender, None, None, body_text,
                          "CAMPAIGN_EXTRACT_FAILED")
        return
    camp = result["campaign"]

    # 4b. duplicate check - same OEM company, same title/objective, still active #
    with session_scope() as db:
        dup = _find_active_duplicate(
            db, oem_company_id, camp.get("title"),
            (camp.get("summary") or {}).get("objective"),
        )
        dup_id, dup_title = (dup.id, dup.title) if dup is not None else (None, None)

    if dup_id is not None:
        subject, body = duplicate_campaign_email(
            recipient_name=oem_user_name, campaign_title=dup_title, campaign_id=dup_id,
        )
        out = _safe_reply(provider, msg, subject, body)
        with session_scope() as db:
            _log_incoming(db, msg, sender, dup_id, None, body_text, "DUPLICATE_CAMPAIGN")
            _log_outgoing(db, dup_id, None, coordinator_address, [sender],
                          subject, body, out, in_reply_to=message_id)
        print(f"[pipeline] {sender}: matches active campaign {dup_id} "
              f"('{dup_title}') - not creating a duplicate")
        return

    suppliers_in = [s for s in (camp.get("supplier_list") or []) if s.get("user_email")]

    # 5. insert campaign + suppliers + inbound conversation ---------- #
    with session_scope() as db:
        campaign_row = Campaign(
            oem_company_id=oem_company_id,
            title=camp.get("title") or (msg.get("subject") or "Untitled campaign"),
            org_email=camp.get("org_email") or sender,
            summary=camp.get("summary") or {},
            addl_email=", ".join(camp.get("addl_email") or []) or None,
            is_active=True,
            total_suppliers=len(suppliers_in),
            started_at=_dt(camp.get("started_at")) or _now(),
            ended_at=_dt(camp.get("ended_at")),
        )
        db.add(campaign_row)
        db.flush()
        campaign_id = campaign_row.id
        summary = campaign_row.summary

        supplier_rows = []
        for s in suppliers_in:
            row = CampaignSupplier(
                campaign_id=campaign_id,
                user_name=s.get("user_name"),
                user_email=str(s["user_email"]).strip().lower(),
                user_phone_number=s.get("user_phone_number"),
                company_name=s.get("company_name"),
                company_domain=s.get("company_domain"),
            )
            db.add(row)
            db.flush()
            supplier_rows.append((row.id, row.user_email, row.user_name))

        _log_incoming(db, msg, sender, campaign_id, None, body_text, "PROCESSED")

    print(f"[pipeline] campaign {campaign_id} inserted "
          f"({len(supplier_rows)} suppliers)")

    # 6. build the supplier mail from the stored summary ------------ #
    draft = _run_mail(summary)
    if draft.get("error") or not draft.get("mail"):
        print(f"[pipeline] mail creation failed: {draft.get('error')}")
        return
    mail = draft["mail"]
    signature = _signature(oem_user_name, sender, oem_user_phone)

    # 7. send to each supplier, one by one ------------------------- #
    sent_ok = 0
    for supplier_id, supplier_email, supplier_name in supplier_rows:
        body = _personalize(mail["mail_content"], supplier_name, signature)
        out = _safe_send(provider, supplier_email, {**mail, "mail_content": body})
        sent_ok += 1 if out.get("ok") else 0
        with session_scope() as db:
            _log_outgoing(db, campaign_id, supplier_id, coordinator_address,
                          [supplier_email], mail["subject"], body,
                          out, cc=mail.get("cc"), bcc=mail.get("bcc"))
    print(f"[pipeline] campaign {campaign_id}: sent {sent_ok}/{len(supplier_rows)} "
          f"supplier email(s)")
