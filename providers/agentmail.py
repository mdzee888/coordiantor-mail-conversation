"""
AgentMail  ->  coordinate-inbox listener
========================================

Delivery : AgentMail webhook, event `message.received`.
Flow     : webhook hits /webhook
           -> fetch the full message from the API
           -> print every field (from / to / cc / bcc / subject / body / ...)
           -> mark it read  (add label "read", remove label "unread")

Commands  (setup also POST /admin/agentmail/<setup|renew|teardown|status>)
--------
    python providers/agentmail.py setup      register the webhook with AgentMail
    python providers/agentmail.py serve      run the webhook receiver   (default)
    python providers/agentmail.py backfill   print + mark-read every currently-unread mail

Required .env keys
------------------
    AGENTMAIL_API_KEY
    AGENTMAIL_INBOX_ID          (falls back to COORDINATE_EMAIL)
    AGENTMAIL_WEBHOOK_URL       public https URL ending in /webhook  (setup only)
    AGENTMAIL_WEBHOOK_SECRET    whsec_... signing secret (optional but recommended)
"""
import json
import os
import sys

# Run either as `python providers/agentmail.py` or `from providers.agentmail import app`.
# Drop this dir from sys.path so `import agentmail` resolves to the installed SDK,
# not this file; add the repo root so `import common` works.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
sys.path.insert(0, os.path.dirname(_HERE))

from flask import Flask, request, jsonify

from agentmail import AgentMail

from common import env, as_list, print_message  # env(): env var -> Secret Manager
from config import config

API_KEY        = env("AGENTMAIL_API_KEY", required=True)
INBOX_ID       = env("AGENTMAIL_INBOX_ID") or env("COORDINATE_EMAIL", required=True)
WEBHOOK_URL    = env("AGENTMAIL_WEBHOOK_URL")
WEBHOOK_SECRET = env("AGENTMAIL_WEBHOOK_SECRET")

# sender display name shown to recipients, e.g. "Zee-Coordinator" instead of the
# inbox's own name - AGENTMAIL_CONFIG["FROM_NAME"]. AgentMail has no per-message
# "from name" - it's a property of the inbox itself, synced via sync_display_name()
# below. Read straight off the whole JSON blob (not through BUNDLED_SECRETS) since
# "FROM_NAME" is also used inside MS365_Mail / GOOGLE_MAIL and each provider's
# value must stay independent.
FROM_NAME = ((config.get_json("AGENTMAIL_CONFIG") or {}).get("FROM_NAME") or "").strip() or None

client = AgentMail(api_key=API_KEY)
app = Flask(__name__)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _to_dict(obj):
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict"):
        if hasattr(obj, attr):
            return getattr(obj, attr)()
    return dict(obj)


def _normalize(m):
    m = _to_dict(m)
    attachments = []
    for a in m.get("attachments") or []:
        a = _to_dict(a) if not isinstance(a, str) else {"filename": a}
        attachments.append(a.get("filename") or a.get("name") or "attachment")
    return {
        "message_id":  m.get("message_id"),
        "thread_id":   m.get("thread_id"),
        "in_reply_to": m.get("in_reply_to"),
        "inbox_id":    m.get("inbox_id"),
        "date":        m.get("timestamp"),
        "from":        as_list(m.get("from_") or m.get("from")),
        "to":          as_list(m.get("to")),
        "cc":          as_list(m.get("cc")),
        "bcc":         as_list(m.get("bcc")),
        "reply_to":    as_list(m.get("reply_to")),
        "subject":     m.get("subject"),
        "labels":      as_list(m.get("labels")),
        "text":        m.get("text"),
        "html":        m.get("html"),
        "attachments": attachments,
    }


def _fetch_full(inbox_id, message_id):
    return client.inboxes.messages.get(inbox_id=inbox_id, message_id=message_id)


def _mark_read(inbox_id, message_id):
    # AgentMail has no read flag; read/unread is tracked with labels.
    client.inboxes.messages.update(
        inbox_id=inbox_id,
        message_id=message_id,
        add_labels=["read"],
        remove_labels=["unread"],
    )


def handle(hook_message):
    """hook_message = the `message` object from the webhook payload."""
    hook_message = _to_dict(hook_message)
    inbox_id = hook_message.get("inbox_id") or INBOX_ID
    message_id = hook_message.get("message_id")

    try:
        full = _fetch_full(inbox_id, message_id)
    except Exception as exc:                       # fall back to the webhook copy
        print(f"[agentmail] fetch failed ({exc}); using webhook payload")
        full = hook_message

    norm = _normalize(full)

    try:
        _mark_read(inbox_id, message_id)
        norm["marked_read"] = True
    except Exception as exc:
        print(f"[agentmail] mark-read failed: {exc}")
        norm["marked_read"] = False

    print_message("agentmail", norm)

    norm.setdefault("inbox_id", inbox_id)
    try:
        from pipeline import process as _run_pipeline
        _run_pipeline(norm, "agentmail", INBOX_ID)
    except Exception as exc:  # noqa: BLE001 - pipeline must never break the webhook
        print(f"[agentmail] pipeline error: {exc}")


# --------------------------------------------------------------------------- #
# outbound (used by mailer.py)
# --------------------------------------------------------------------------- #
def send_mail(*, to, subject, text, cc=None, bcc=None):
    resp = client.inboxes.messages.send(
        inbox_id=INBOX_ID, to=to, cc=cc or None, bcc=bcc or None,
        subject=subject, text=text,
    )
    d = _to_dict(resp)
    return {"message_id": d.get("message_id"), "thread_id": d.get("thread_id")}


def reply_mail(original, *, text, subject=None, cc=None, bcc=None):
    resp = client.inboxes.messages.reply(
        inbox_id=original.get("inbox_id") or INBOX_ID,
        message_id=original["message_id"],
        text=text,
        cc=cc or None,
        bcc=bcc or None,
    )
    d = _to_dict(resp)
    return {"message_id": d.get("message_id"), "thread_id": d.get("thread_id")}


def _verify(req):
    """Svix signature check. Skipped when no secret is configured."""
    if not WEBHOOK_SECRET:
        return True
    try:
        from svix.webhooks import Webhook
        headers = {k.lower(): v for k, v in req.headers.items()}
        Webhook(WEBHOOK_SECRET).verify(req.get_data(), headers)
        return True
    except Exception as exc:
        print(f"[agentmail] signature verification failed: {exc}")
        return False


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.post("/webhook")
def webhook():
    if not _verify(request):
        return "invalid signature", 401
    event = request.get_json(force=True, silent=True) or {}
    if event.get("event_type", "").startswith("message.received") and event.get("message"):
        handle(event["message"])
    return jsonify(ok=True)


@app.get("/health")
def health():
    return jsonify(ok=True, provider="agentmail", inbox=INBOX_ID)


# --------------------------------------------------------------------------- #
# admin ops  (CLI + HTTP /admin/agentmail/<action> via main.py)
# --------------------------------------------------------------------------- #
def register_webhook() -> dict:
    if not WEBHOOK_URL:
        raise RuntimeError("AGENTMAIL_WEBHOOK_URL is not set")
    wh = _to_dict(client.webhooks.create(
        url=WEBHOOK_URL, event_types=["message.received"]))
    return {"provider": "agentmail", "action": "register_webhook", "webhook": wh}


def list_webhooks() -> dict:
    res = _to_dict(client.webhooks.list())
    return {"provider": "agentmail", "webhooks": res.get("webhooks", res)}


def sync_display_name() -> dict:
    """Push AGENTMAIL_CONFIG['FROM_NAME'] onto the inbox's display_name, so it's what
    recipients see in the From line instead of the inbox's own name/address."""
    if not FROM_NAME:
        return {"provider": "agentmail", "action": "sync_display_name",
                "note": "FROM_NAME not set - inbox display name left unchanged"}
    inbox = _to_dict(client.inboxes.update(inbox_id=INBOX_ID, display_name=FROM_NAME))
    return {"provider": "agentmail", "action": "sync_display_name",
            "display_name": inbox.get("display_name")}


def admin_op(action: str) -> dict:
    if action == "setup":
        return {"provider": "agentmail", "action": "setup",
                "webhook": register_webhook().get("webhook"),
                "display_name": sync_display_name().get("display_name")}
    if action == "renew":
        # webhooks don't expire, but re-sync the display name in case FROM_NAME changed
        return sync_display_name()
    if action == "status":
        res = list_webhooks()
        inbox = _to_dict(client.inboxes.get(inbox_id=INBOX_ID))
        res["display_name"] = inbox.get("display_name")
        return res
    if action == "teardown":
        return {"provider": "agentmail", "action": "teardown",
                "note": "delete the webhook from the AgentMail dashboard"}
    raise ValueError(f"agentmail: unknown admin action {action!r}")


def cmd_setup():
    out = register_webhook()
    print(json.dumps(out, indent=2, default=str))
    print("\n-> copy the signing secret into AGENTMAIL_CONFIG (AGENTMAIL_WEBHOOK_SECRET)")
    print(json.dumps(sync_display_name(), indent=2, default=str))


def cmd_backfill():
    res = client.inboxes.messages.list(inbox_id=INBOX_ID)
    res = _to_dict(res)
    for m in res.get("messages", []):
        md = _to_dict(m)
        if "unread" in as_list(md.get("labels")):
            handle(md)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "setup":
        cmd_setup()
    elif cmd == "backfill":
        cmd_backfill()
    else:
        app.run(host=env("HOST", "0.0.0.0"), port=int(env("PORT", "8080")))


if __name__ == "__main__":
    main()
