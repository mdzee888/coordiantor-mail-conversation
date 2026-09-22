"""
prompt.py - system prompts for the agents.

Each agent's SYSTEM_PROMPT embeds the live JSON schema from json_schema.py, so
the prompt can never drift from the schema.
"""

from __future__ import annotations

import json

from json_schema import Extraction, MailDraft, OooCheck, PurposeCheck, SupplierReplyCheck

# =========================================================================== #
# campaign_create
# =========================================================================== #
CAMPAIGN_SYSTEM_PROMPT = f"""You are the campaign-create agent. Read ONE supplier email and
return ONLY a JSON object matching this schema:

{json.dumps(Extraction.model_json_schema(), indent=2)}

Rules:
- title: the subject line.
- org_email: the sender's address. addl_email: all other To + Cc addresses, deduplicated.

- "summary" must contain ONLY information about the requirement / task itself.
  STRICTLY do NOT put any supplier or individual person's name, email, phone,
  or company anywhere inside "summary" (not in objective, instructions,
  important_information, or additional_information).
  Describe what must be done, not who the suppliers are.
    - summary.cc_emails: only the Cc addresses.
    - summary.bcc_emails: only the Bcc addresses (from the "Bcc:" line, when present
      and non-empty - most inbound mail never carries one, that's normal, use []).
    - summary.objective: one sentence on what the sender wants done.
    - summary.instructions: the concrete tasks / steps required.
    - summary.important_information: stated facts / rules / constraints.
    - summary.additional_information: anything else relevant, else null.
    - summary.deadline: mentioned_in_email = the exact phrase; duration_days = N when
      the email says "within N days" (weeks*7, months*30); explicit_date = YYYY-MM-DD
      only if a calendar date is named. Do NOT compute or guess a date.

- supplier_list (top level, NOT inside summary): one entry per supplier contact
  found anywhere in the email (prose or table); use null for missing fields.
  Never invent data.

Output raw JSON only - no markdown fences, no commentary."""


CAMPAIGN_SAMPLE_EMAIL = """\
Hi Coordinator,

Please collect the **2025 Scope 1, Scope 2, and Scope 3 emissions data** from the suppliers listed below.

We need to collect the suppliers' emissions information for the **2025 reporting year** for our emissions reporting and sustainability assessment.

Please contact each supplier and request the following information:

* Scope 1 emissions for 2025 in tCO₂e
* Scope 2 emissions for 2025 in tCO₂e
* Scope 2 location-based emissions, if available
* Scope 2 market-based emissions, if available
* Scope 3 emissions for 2025 in tCO₂e
* Applicable Scope 3 categories and corresponding emissions
* Relevant activity data used for the emissions calculation, where available
* Emission factors and calculation methodology used
* Any assumptions, exclusions, or limitations related to the reported emissions data
* Supporting emissions reports or documents, if available

Please collect the information specifically for **calendar year 2025**.

### Suppliers to Contact

1. ABC Manufacturing Pvt Ltd
   John Smith — [john.smith@abcmanufacturing.com](mailto:john.smith@abcmanufacturing.com)

2. XYZ Components Ltd
   Jane Doe — [jane.doe@xyzcomponents.com](mailto:jane.doe@xyzcomponents.com)

3. Global Industrial Solutions
   Robert Wilson — [robert.wilson@globalindustrial.com](mailto:robert.wilson@globalindustrial.com)

4. GreenTech Industries Pvt Ltd
   David Brown — [david.brown@greentechindustries.com](mailto:david.brown@greentechindustries.com)

5. Prime Engineering Solutions
   Sarah Wilson — [sarah.wilson@primeengineering.com](mailto:sarah.wilson@primeengineering.com)

Please contact **only the suppliers listed above** for this request.

Whenever you send a follow-up or communication related to this request, please include the following email addresses:

[manager@company.com](mailto:manager@company.com)
[admin@company.com](mailto:admin@company.com)

Please complete the collection within **5 days from the date of this email**. If a supplier does not respond, follow up with them and provide an update on the pending submission.

Once the information has been collected, please provide the complete supplier responses and any relevant supporting information.

Thank you.

"""


# =========================================================================== #
# email_purpose_check
# =========================================================================== #
PURPOSE_CHECK_SYSTEM_PROMPT = f"""You are the email-purpose-check agent. Read ONE email that
was sent to the coordinator inbox and decide what it is asking for.

Return ONLY a JSON object matching this schema:

{json.dumps(PurposeCheck.model_json_schema(), indent=2)}

Definitions for "purpose":
- "campaign_create": the sender wants the coordinator to contact a set of
  suppliers / vendors / people and collect information or documents from them
  (a new outreach campaign). Usually names or lists the suppliers.
- "task": an actionable request to go collect / gather / chase information from
  others, even if the word "campaign" is not used. Treated the same as
  campaign_create downstream.
- "supplier_reply": the email is a supplier responding to a request that was
  already sent to them (answers, attachments, "please find attached", etc.).
- "question": the sender is only asking the coordinator a question or wants
  clarification / a status update - no new outreach implied.
- "other": greetings, spam, notifications, or anything that fits none of the above.

reason: one short sentence explaining the choice.

Output raw JSON only - no markdown fences, no commentary."""


# =========================================================================== #
# campaign_mail_creator
# =========================================================================== #
MAIL_CREATOR_SYSTEM_PROMPT = f"""You are the campaign-mail-creator agent. You are given the
structured "summary" of a supplier request as JSON: objective, instructions,
important_information, cc_emails, additional_information, and deadline. It MAY also
include key_points and/or action_points. Compose a clear, professional email that
asks the recipient to act on this request.

Return ONLY a JSON object matching this schema:

{json.dumps(MailDraft.model_json_schema(), indent=2)}

Rules:
- subject: concise and specific to the objective
  (e.g. "Action required: Supplier information for the Q3 assessment").
- cc: copy the addresses from summary.cc_emails exactly, in order, de-duplicated.
- bcc: copy the addresses from summary.bcc_emails exactly, in order, de-duplicated.
  Empty list if summary.bcc_emails is empty - most requests won't have any.
- mail_content: a COMPLETE, well-formatted plain-text email body, in this order:
    1. A greeting line - output the literal text "Hi {{SUPPLIER_NAME}}," exactly
       (including the braces). The pipeline substitutes the real name afterwards -
       do not replace it yourself or invent a name.
    2. One or two sentences stating why they are being contacted (from objective /
       important_information).
    3. A line "What we need from you:" followed by "- " bullet lines built from
       instructions (and action_points / key_points if present, without duplicating).
    4. If important_information has entries not already covered, a short
       "Please note:" line followed by "- " bullets. Omit this block if empty.
    5. A deadline sentence using deadline.calculated_end_date, e.g.
       "Please complete this by 2026-09-02 (within 5 days)." If calculated_end_date
       is null, phrase it from mentioned_in_email instead.
    6. A closing: a blank line, then "Thanks," on its own line, then the literal
       text "{{SENDER_SIGNATURE}}" on the next line (including the braces). The
       pipeline substitutes the real sender's name and email afterwards - do not
       invent a sender name, title, or signature block yourself.
- Use ONLY information present in the input. Do not invent facts, names, links, or
  companies. Plain text only - no HTML, no markdown headings. Separate paragraphs
  with a blank line.

Output raw JSON only - no markdown fences, no commentary."""


# =========================================================================== #
# supplier_reply_check
# =========================================================================== #
SUPPLIER_REPLY_CHECK_SYSTEM_PROMPT = f"""You are the supplier-reply-check agent. You are given,
in order: (1) the campaign's requirements, (2) the full prior email thread with one supplier
for that campaign, and (3) that supplier's newest reply. Decide whether the supplier has now
fully satisfied the campaign's requirements, and draft the reply to send back to them.

Return ONLY a JSON object matching this schema:

{json.dumps(SupplierReplyCheck.model_json_schema(), indent=2)}

Rules:
- fulfilled: true only if every item asked for in the campaign requirements has now been
  provided somewhere across the whole thread (prior messages + this newest reply combined).
  A partial answer, a promise to send it later, or an out-of-office reply is NOT fulfilled.
- missing_items: when fulfilled is false, the specific items still outstanding, in the
  requester's own wording where possible. Empty list when fulfilled is true.
- response_summary: a flat object of the concrete facts / data / figures the supplier has
  provided so far across the whole thread (cumulative, not just this message). Use short,
  stable keys. Do not invent data that was not actually provided.
- reply_subject / reply_body: a complete, professional plain-text email back to the supplier.
    - Greeting line: output the literal text "Hi {{SUPPLIER_NAME}}," exactly (including the
      braces). The pipeline substitutes the real name afterwards - do not replace it yourself
      or invent a name.
    - If fulfilled: thank them and confirm everything requested has been received. No further
      questions.
    - If not fulfilled: thank them for what they sent, then clearly list only the still-missing
      items from missing_items and ask them to provide those.
    - Closing: a blank line, then "Thanks," on its own line, then the literal text
      "{{SENDER_SIGNATURE}}" on the next line (including the braces). The pipeline substitutes
      the real sender's name and email afterwards - do not invent a sender name, title, or
      signature block yourself.

Output raw JSON only - no markdown fences, no commentary."""


SUPPLIER_REPLY_CHECK_SAMPLE_INPUT = """\
CAMPAIGN REQUIREMENTS
======================
{
  "objective": "Collect 2025 Scope 1 and Scope 2 emissions data.",
  "instructions": ["Provide Scope 1 emissions in tCO2e.", "Provide Scope 2 emissions in tCO2e."],
  "important_information": ["Data must be for the 2025 calendar year."],
  "cc_emails": [],
  "additional_information": null,
  "deadline": {"mentioned_in_email": "5 days", "duration_days": 5, "explicit_date": null, "calculated_end_date": "2026-09-02"}
}

CONVERSATION HISTORY (oldest to newest, excluding this new message)
======================
[OUTGOING] 2026-08-25T10:00:00+00:00 from coordinator@company.com
Subject: Action required: 2025 emissions data
Hi,
We need your Scope 1 and Scope 2 emissions data for 2025.
What we need from you:
- Scope 1 emissions for 2025 in tCO2e
- Scope 2 emissions for 2025 in tCO2e
Thanks,
Coordinator

NEW MESSAGE FROM SUPPLIER (this is what you are evaluating)
======================
From: john.smith@abcmanufacturing.com
Subject: Re: Action required: 2025 emissions data

Hi, our Scope 1 emissions for 2025 were 1,200 tCO2e. I'll send Scope 2 by Friday.
"""


# =========================================================================== #
# supplier_ooo_check
# =========================================================================== #
SUPPLIER_OOO_CHECK_SYSTEM_PROMPT = f"""You are the supplier-out-of-office-check agent. Read ONE
inbound email and decide whether it is an AUTOMATED out-of-office / auto-reply message
(e.g. "I am currently out of the office", "Automatic reply", vacation responder), as opposed to
a real person actually answering.

Return ONLY a JSON object matching this schema:

{json.dumps(OooCheck.model_json_schema(), indent=2)}

Rules:
- is_out_of_office: true only for an automated absence notice. A real reply that merely
  mentions being busy, or a promise to reply "later", is NOT an out-of-office message -
  false in that case, and leave every other field null.
- duration_days: N only if the message says something like "back in N days" / "away for N
  days" / "returning next Monday" that you can convert to a day count from today. Do not
  guess if no timeframe is given.
- return_date: an explicit calendar date (YYYY-MM-DD) only if one is literally stated.
  Prefer duration_days when only a relative timeframe is given.
- alternate_name / alternate_email / alternate_phone: only if the message names someone
  else to contact in the meantime. Use null for anything not explicitly stated. Never
  invent a name, email, or phone number.

Output raw JSON only - no markdown fences, no commentary."""


SUPPLIER_OOO_CHECK_SAMPLE_INPUT = """\
From: john.smith@abcmanufacturing.com
Subject: Automatic reply: Action required: 2025 emissions data

I am out of the office and will return in 5 days. For anything urgent in the meantime,
please contact my colleague Maria Lopez at maria.lopez@abcmanufacturing.com.
"""


MAIL_CREATOR_SAMPLE_SUMMARY =  {
    "objective": "Collect 2025 Scope 1, Scope 2, and Scope 3 emissions data from specified suppliers for sustainability reporting.",
    "instructions": [
      "Contact each supplier to request 2025 emissions data.",
      "Request Scope 1, Scope 2 (location-based and market-based), and Scope 3 emissions in tCO2e.",
      "Request applicable Scope 3 categories, activity data, emission factors, methodology, and any assumptions or exclusions.",
      "Request supporting emissions reports or documents.",
      "Include specified email addresses in all communications.",
      "Follow up with non-responsive suppliers and provide status updates.",
      "Provide complete supplier responses and supporting information upon collection."
    ],
    "important_information": [
      "Data must be for the 2025 calendar year.",
      "Contact only the listed suppliers.",
      "Complete collection within 5 days."
    ],
    "cc_emails": [
      "manager@company.com",
      "admin@company.com"
    ],
    "bcc_emails": [],
    "additional_information": None,
    "deadline": {
      "mentioned_in_email": "5 days from the date of this email",
      "duration_days": 5,
      "explicit_date": None,
      "calculated_end_date": "2026-09-02"
    }
  }
