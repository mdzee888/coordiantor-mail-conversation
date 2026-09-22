"""
campaign.py - "campaign create" agent: a supplier email -> DB-ready JSON.

Flow (LangGraph):   extract (LLM)  ->  finalize (pure Python, UTC)

Files:
    json_schema.py  - the Pydantic / JSON schema (all agents)
    prompt.py       - the system prompts (all agents)
    campaign.py     - the agent, finalize step, and graph (this file)

Run:
    python campaign.py                    # the sample email
    python campaign.py path/to/email.txt  # a file's contents
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from agentkit import BaseAgent  # importing this also wires config (env -> Secret Manager)
from json_schema import Campaign, Extraction
from prompt import CAMPAIGN_SAMPLE_EMAIL, CAMPAIGN_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #
class CampaignAgent(BaseAgent):
    name = "campaign_create"          # env prefix: CAMPAIGN_CREATE_MODEL, ...
    schema = Extraction
    system_prompt = CAMPAIGN_SYSTEM_PROMPT


campaign_agent = CampaignAgent()


# --------------------------------------------------------------------------- #
# finalize - deterministic, no LLM, all UTC
# --------------------------------------------------------------------------- #
def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def finalize(extraction: Extraction) -> Campaign:
    now = datetime.now(timezone.utc)
    s = extraction.summary

    # deadline hints -> a concrete end datetime
    end: Optional[datetime] = None
    days = s.deadline.duration_days
    if days is not None:
        end = now + timedelta(days=days)
    elif s.deadline.explicit_date:
        try:
            d = datetime.strptime(s.deadline.explicit_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end = d.replace(hour=now.hour, minute=now.minute, second=now.second)
            days = (end.date() - now.date()).days
        except ValueError:
            pass
    s.deadline.duration_days = days
    s.deadline.calculated_end_date = end.date().isoformat() if end else None

    # keep only suppliers with an email (DB column is NOT NULL)
    suppliers = [
        sup.model_copy(update={"user_email": sup.user_email.strip().lower()})
        for sup in extraction.supplier_list
        if sup.user_email and sup.user_email.strip()
    ]

    return Campaign(
        title=extraction.title,
        org_email=extraction.org_email,
        addl_email=extraction.addl_email,
        summary=s,
        supplier_list=suppliers,
        is_active=True,
        total_suppliers=len(suppliers),
        started_at=_utc_iso(now),
        ended_at=_utc_iso(end) if end else None,
    )


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
class State(TypedDict, total=False):
    email: str
    extraction: dict
    campaign: dict
    meta: dict
    error: str


def _finalize_node(state: State) -> State:
    if state.get("error"):
        return {}
    if not state.get("extraction"):
        return {"error": "finalize: nothing extracted"}
    campaign = finalize(Extraction.model_validate(state["extraction"]))
    return {"campaign": campaign.model_dump(mode="json")}


def build_workflow():
    g = StateGraph(State)
    g.add_node("extract", campaign_agent.node(read="email", write="extraction"))
    g.add_node("finalize", _finalize_node)
    g.add_edge(START, "extract")
    g.add_edge("extract", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


WORKFLOW = build_workflow()


def run(email: str) -> State:
    """Email text -> final state ({'campaign': {...}} or {'error': '...'})."""
    return WORKFLOW.invoke({"email": email})


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    email = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else CAMPAIGN_SAMPLE_EMAIL
    out = run(email)
    if out.get("error"):
        print("FAILED:", out["error"])
        raise SystemExit(1)
    print(json.dumps(out["campaign"], indent=2))

    for agent, m in (out.get("meta") or {}).items():
        cost = m.get("cost_usd")
        print("-" * 60)
        print(f"agent         : {agent}")
        print(f"model         : {m.get('model')}")
        print(f"input tokens  : {m.get('input_tokens')}")
        print(f"output tokens : {m.get('output_tokens')}")
        print(f"total tokens  : {m.get('total_tokens')}")
        print(f"cost (USD)    : {f'${cost:.6f}' if cost is not None else 'n/a'}")
