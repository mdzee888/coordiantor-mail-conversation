"""
email_purpose_check.py - classify one inbound coordinator email.

Given the raw text of an email sent to the coordinator inbox, returns:
    {"purpose": "campaign_create" | "task" | "supplier_reply" | "question" | "other",
     "reason": "..."}
or {"error": "..."} if the model call fails.

Files:
    json_schema.py  - PurposeCheck schema
    prompt.py       - PURPOSE_CHECK_SYSTEM_PROMPT
    email_purpose_check.py - the agent + run() (this file)

Run:
    python email_purpose_check.py                 # the campaign sample email
    python email_purpose_check.py path/to/mail.txt
"""

from __future__ import annotations

import json
import sys

from agentkit import AgentError, BaseAgent
from json_schema import PurposeCheck
from prompt import CAMPAIGN_SAMPLE_EMAIL, PURPOSE_CHECK_SYSTEM_PROMPT

#: purposes that should trigger the campaign pipeline
CAMPAIGN_PURPOSES = ("campaign_create", "task")


class PurposeCheckAgent(BaseAgent):
    name = "email_purpose_check"        # env prefix: EMAIL_PURPOSE_CHECK_MODEL, ...
    schema = PurposeCheck
    system_prompt = PURPOSE_CHECK_SYSTEM_PROMPT


purpose_agent = PurposeCheckAgent()


def run(email_text: str) -> dict:
    """Email text -> {'purpose': ..., 'reason': ...}  or  {'error': ...}."""
    try:
        data = purpose_agent.run(email_text)
    except AgentError as exc:
        return {"error": str(exc)}
    return data.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    email = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else CAMPAIGN_SAMPLE_EMAIL
    out = run(email)
    print(json.dumps(out, indent=2))
    if out.get("error"):
        raise SystemExit(1)
    m = purpose_agent.last_run
    print("-" * 60)
    print(f"model         : {m.get('model')}")
    print(f"total tokens  : {m.get('total_tokens')}")
    cost = m.get("cost_usd")
    print(f"cost (USD)    : {f'${cost:.6f}' if cost is not None else 'n/a'}")
