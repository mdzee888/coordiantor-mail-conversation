"""
supplier_ooo_check.py - detect whether an inbound supplier email is an automated
out-of-office / auto-reply, and if so extract the return timing + alternate contact.

Given the raw text of ONE inbound email, returns:
    {"is_out_of_office": bool, "duration_days": int | None, "return_date": "YYYY-MM-DD" | None,
     "alternate_name": ..., "alternate_email": ..., "alternate_phone": ...}
or {"error": "..."} if the model call fails.

Files:
    json_schema.py       - OooCheck schema
    prompt.py             - SUPPLIER_OOO_CHECK_SYSTEM_PROMPT
    supplier_ooo_check.py - the agent + run() (this file)

Run:
    python supplier_ooo_check.py                 # the sample auto-reply
    python supplier_ooo_check.py path/to/mail.txt
"""

from __future__ import annotations

import json
import sys

from agentkit import AgentError, BaseAgent
from json_schema import OooCheck
from prompt import SUPPLIER_OOO_CHECK_SAMPLE_INPUT, SUPPLIER_OOO_CHECK_SYSTEM_PROMPT


class SupplierOooCheckAgent(BaseAgent):
    name = "supplier_ooo_check"        # env prefix: SUPPLIER_OOO_CHECK_MODEL, ...
    schema = OooCheck
    system_prompt = SUPPLIER_OOO_CHECK_SYSTEM_PROMPT


supplier_ooo_agent = SupplierOooCheckAgent()


def run(email_text: str) -> dict:
    """One inbound email -> out-of-office dict, or {'error': ...}."""
    try:
        data = supplier_ooo_agent.run(email_text)
    except AgentError as exc:
        return {"error": str(exc)}
    return data.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    text = (open(sys.argv[1], encoding="utf-8").read()
            if len(sys.argv) > 1 else SUPPLIER_OOO_CHECK_SAMPLE_INPUT)
    out = run(text)
    print(json.dumps(out, indent=2))
    if out.get("error"):
        raise SystemExit(1)
    m = supplier_ooo_agent.last_run
    print("-" * 60)
    print(f"model         : {m.get('model')}")
    print(f"total tokens  : {m.get('total_tokens')}")
    cost = m.get("cost_usd")
    print(f"cost (USD)    : {f'${cost:.6f}' if cost is not None else 'n/a'}")
