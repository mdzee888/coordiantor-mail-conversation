"""Shared helpers for the three coordinate-inbox listeners.

Each provider script (agentmail_inbox.py / gmail_inbox.py / m365_inbox.py) turns a
received email into the same normalized dict and hands it to `print_message`, so
the console output looks identical no matter which provider delivered the mail.

Normalized message dict:
    {
      "message_id":  str,
      "date":        str,
      "from":        [str, ...],
      "to":          [str, ...],
      "cc":          [str, ...],
      "bcc":         [str, ...],
      "reply_to":    [str, ...],
      "subject":     str,
      "labels":      [str, ...],
      "text":        str,          # plain-text body
      "html":        str,          # html body (if any)
      "attachments": [str, ...],   # file names
      "marked_read": bool,         # set by the handler after it marks the mail read
    }
"""
from datetime import datetime, timezone

from config import config


def env(key, default=None, required=False):
    """Resolve a config value: env var -> GCP Secret Manager (see config.py)."""
    if required:
        return config.require(key)
    return config.get(key, default)


def as_list(value):
    """Coerce None / str / iterable into a plain list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [v for v in value if v]


def print_message(provider, msg):
    """Pretty-print a normalized message dict to stdout."""
    bar = "=" * 72
    print(bar)
    print(f"[{provider}] NEW MAIL   received-hook-at {datetime.now(timezone.utc).isoformat()}")
    print(bar)
    print(f"  message-id : {msg.get('message_id')}")
    print(f"  date       : {msg.get('date')}")
    print(f"  from       : {', '.join(as_list(msg.get('from')))}")
    print(f"  to         : {', '.join(as_list(msg.get('to')))}")
    print(f"  cc         : {', '.join(as_list(msg.get('cc')))}")
    print(f"  bcc        : {', '.join(as_list(msg.get('bcc')))}")
    print(f"  reply-to   : {', '.join(as_list(msg.get('reply_to')))}")
    print(f"  subject    : {msg.get('subject')}")
    print(f"  labels     : {', '.join(as_list(msg.get('labels')))}")

    atts = as_list(msg.get("attachments"))
    print(f"  attachments: {len(atts)}")
    for name in atts:
        print(f"      - {name}")

    body = (msg.get("text") or "").strip()
    if not body and msg.get("html"):
        body = "[html body only, showing first 800 chars]\n" + msg["html"][:800]
    print("  ---- body " + "-" * 62)
    for line in (body.splitlines() or [""]):
        print(f"  {line}")
    print("  " + "-" * 70)
    print(f"  marked as read : {msg.get('marked_read')}")
    print(bar, flush=True)
