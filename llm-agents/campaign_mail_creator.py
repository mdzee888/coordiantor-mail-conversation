"""
campaign_mail_creator.py - turn a campaign summary into a ready-to-send email.

Flow (LangGraph):   compose (LLM)  ->  finalize (pure Python)

Input : the campaign "summary" object (json_schema.Summary), or any dict / JSON with
        objective / instructions / important_information / cc_emails / deadline
        (key_points / action_points optional).
Output: {"subject": ..., "cc": [...], "bcc": [], "mail_content": "..."}

Files:
    json_schema.py  - MailDraft schema
    prompt.py       - MAIL_CREATOR_SYSTEM_PROMPT
    campaign_mail_creator.py - the agent + graph (this file)

Run:
    python campaign_mail_creator.py                 # the sample summary
    python campaign_mail_creator.py summary.json    # a JSON file's contents

Chain from campaign.py:
    from campaign import run as run_campaign
    from campaign_mail_creator import run as run_mail
    campaign = run_campaign(email)["campaign"]
    draft = run_mail(campaign["summary"])["mail"]
"""

from __future__ import annotations

import json
import sys
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from agentkit import BaseAgent  # importing this also wires config (env -> Secret Manager)
from json_schema import MailDraft
from prompt import MAIL_CREATOR_SAMPLE_SUMMARY, MAIL_CREATOR_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #
class MailCreatorAgent(BaseAgent):
    name = "campaign_mail_creator"     # env prefix: CAMPAIGN_MAIL_CREATOR_MODEL, ...
    schema = MailDraft
    system_prompt = MAIL_CREATOR_SYSTEM_PROMPT


mail_creator_agent = MailCreatorAgent()


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
class State(TypedDict, total=False):
    summary: dict          # input
    mail: dict             # output: MailDraft dump
    meta: dict
    error: str


def _finalize_node(state: State) -> State:
    """Make cc / bcc authoritative from the input - never trust the model for routing."""
    if state.get("error"):
        return {}
    mail = dict(state.get("mail") or {})
    if not mail:
        return {"error": "campaign_mail_creator: nothing produced"}
    summary = state.get("summary", {})
    cc = summary.get("cc_emails") or []
    bcc = summary.get("bcc_emails") or []
    mail["cc"] = list(dict.fromkeys(e.strip().lower() for e in cc if e and e.strip()))
    mail["bcc"] = list(dict.fromkeys(e.strip().lower() for e in bcc if e and e.strip()))
    return {"mail": mail}


def build_workflow():
    g = StateGraph(State)
    g.add_node("compose", mail_creator_agent.node(read="summary", write="mail"))
    g.add_node("finalize", _finalize_node)
    g.add_edge(START, "compose")
    g.add_edge("compose", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


WORKFLOW = build_workflow()


def run(summary: dict) -> State:
    """Campaign summary -> final state ({'mail': {...}} or {'error': '...'})."""
    return WORKFLOW.invoke({"summary": summary})


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    if len(sys.argv) > 1:
        summary = json.loads(open(sys.argv[1], encoding="utf-8").read())
    else:
        summary = MAIL_CREATOR_SAMPLE_SUMMARY

    out = run(summary)
    if out.get("error"):
        print("FAILED:", out["error"])
        raise SystemExit(1)

    print(json.dumps(out["mail"], indent=2))
    print("=" * 60)
    print(out["mail"]["mail_content"])

    for agent, m in (out.get("meta") or {}).items():
        cost = m.get("cost_usd")
        print("-" * 60)
        print(f"agent         : {agent}")
        print(f"model         : {m.get('model')}")
        print(f"input tokens  : {m.get('input_tokens')}")
        print(f"output tokens : {m.get('output_tokens')}")
        print(f"total tokens  : {m.get('total_tokens')}")
        print(f"cost (USD)    : {f'${cost:.6f}' if cost is not None else 'n/a'}")
