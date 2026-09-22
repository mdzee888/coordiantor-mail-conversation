"""
supplier_reply_check.py - decide whether a supplier's reply fulfills a campaign's
requirements, and draft the reply to send back to them.

Given (1) the campaign's requirement summary, (2) the full prior conversation with
that supplier, and (3) their latest incoming email - all as one text blob - returns:
    {"fulfilled": bool, "missing_items": [...], "response_summary": {...},
     "reply_subject": "...", "reply_body": "..."}
or {"error": "..."} if the model call fails.

Files:
    json_schema.py         - SupplierReplyCheck schema
    prompt.py               - SUPPLIER_REPLY_CHECK_SYSTEM_PROMPT
    supplier_reply_check.py - the agent + run() (this file)

Run:
    python supplier_reply_check.py                 # the sample thread
    python supplier_reply_check.py path/to/thread.txt
"""

from __future__ import annotations

import json
import sys

from agentkit import AgentError, BaseAgent
from json_schema import SupplierReplyCheck
from prompt import SUPPLIER_REPLY_CHECK_SAMPLE_INPUT, SUPPLIER_REPLY_CHECK_SYSTEM_PROMPT


class SupplierReplyCheckAgent(BaseAgent):
    name = "supplier_reply_check"        # env prefix: SUPPLIER_REPLY_CHECK_MODEL, ...
    schema = SupplierReplyCheck
    system_prompt = SUPPLIER_REPLY_CHECK_SYSTEM_PROMPT


supplier_reply_agent = SupplierReplyCheckAgent()


def run(thread_text: str) -> dict:
    """Campaign requirements + conversation thread + new reply -> fulfillment dict, or {'error': ...}."""
    try:
        data = supplier_reply_agent.run(thread_text)
    except AgentError as exc:
        return {"error": str(exc)}
    return data.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    text = (open(sys.argv[1], encoding="utf-8").read()
            if len(sys.argv) > 1 else SUPPLIER_REPLY_CHECK_SAMPLE_INPUT)
    out = run(text)
    print(json.dumps(out, indent=2))
    if out.get("error"):
        raise SystemExit(1)
    m = supplier_reply_agent.last_run
    print("-" * 60)
    print(f"model         : {m.get('model')}")
    print(f"total tokens  : {m.get('total_tokens')}")
    cost = m.get("cost_usd")
    print(f"cost (USD)    : {f'${cost:.6f}' if cost is not None else 'n/a'}")
