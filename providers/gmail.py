"""
Gmail / Google Workspace  ->  coordinate-inbox listener
=======================================================

Delivery : Gmail API  users.watch()  ->  Cloud Pub/Sub  ->  push to /webhook
Flow     : Pub/Sub push  (emailAddress + historyId)
           -> history.list(startHistoryId, messageAdded)
           -> messages.get(format="full")  for each new INBOX message
           -> print every field (from / to / cc / bcc / subject / body / ...)
           -> messages.modify(removeLabelIds=["UNREAD"])   # mark read

Everything comes from ONE JSON secret, GOOGLE_MAIL (config.get_json):
    {
      "coordinator_mail": "...",          # display only
      "client_id":     "...",             # OAuth refresh-token credentials -
      "client_secret": "...",             #   no client-secret file, no local
      "refresh_token": "...",             #   consent
      "token_uri":     "https://oauth2.googleapis.com/token",
      "pubsub_topic":  "projects/<proj>/topics/<topic>",   # required for `setup`
      "push_token":    "<optional>"       # shared secret for the push URL;
                                          #   auto-generated if omitted
    }

Commands  (setup/stop also POST /admin/gmail/<setup|renew|teardown|status>)
--------
    python providers/gmail.py check      connect with GOOGLE_MAIL creds, print the profile
    python providers/gmail.py setup      call users.watch()  (must be re-run every ~7 days)
    python providers/gmail.py stop       call users.stop()
    python providers/gmail.py serve      run the Pub/Sub push receiver   (default)

The history cursor is kept in a fixed local file (gmail_last_history.json); no
env var. The Pub/Sub push subscription points at:
    https://<host>/webhook?token=<GOOGLE_MAIL.push_token>
(only enforced when push_token is set explicitly).
"""
import base64
import json
import os
import secrets
import sys

# Run either as `python providers/gmail.py` or `from providers.gmail import app`.
# Drop this dir from sys.path (so the filename can't shadow a package) and add
# the repo root so `import common` works.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
sys.path.insert(0, os.path.dirname(_HERE))

from flask import Flask, request, jsonify

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from common import env, as_list, print_message  # env(): env var -> Secret Manager
from config import config

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Local cursor file for the Pub/Sub historyId - fixed path, no env var.
HISTORY_FILE = "gmail_last_history.json"

_GM = config.get_json("GOOGLE_MAIL") or {}

# Pub/Sub topic that Gmail's users.watch() publishes to.
PUBSUB_TOPIC = _GM.get("pubsub_topic")

# Shared secret for the push URL - from GOOGLE_MAIL["push_token"] if set,
# otherwise a per-process random fallback. The /webhook check is only enforced
# when the value is explicit; a random token could never match the Pub/Sub
# subscription URL, so enforcing it would 403 every legitimate push.
_PUSH_TOKEN_CONFIGURED = _GM.get("push_token")
PUSH_TOKEN = _PUSH_TOKEN_CONFIGURED or secrets.token_hex(32)

# mailbox address (display only - all API calls use userId="me")
USER = _GM.get("coordinator_mail") or env("COORDINATE_EMAIL")

# sender display name shown to recipients, e.g. "Zee-Coordinator" instead of the
# Google account name - GOOGLE_MAIL["FROM_NAME"]
FROM_NAME = (_GM.get("FROM_NAME") or "").strip() or None

app = Flask(__name__)

_creds = None  # cached OAuth credentials; refreshed on expiry


# --------------------------------------------------------------------------- #
# auth / service
# --------------------------------------------------------------------------- #
def get_service():
    """Gmail client authorised with the GOOGLE_MAIL refresh-token credentials."""
    global _creds
    if _creds is None:
        gm = config.require_json("GOOGLE_MAIL")
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not gm.get(k)]
        if missing:
            raise RuntimeError(f"GOOGLE_MAIL secret missing fields: {', '.join(missing)}")
        _creds = Credentials(
            token=None,
            refresh_token=gm["refresh_token"],
            client_id=gm["client_id"],
            client_secret=gm["client_secret"],
            token_uri=gm.get("token_uri", "https://oauth2.googleapis.com/token"),
            scopes=SCOPES,
        )
    if not _creds.valid:
        _creds.refresh(Request())
    return build("gmail", "v1", credentials=_creds, cache_discovery=False)


# --------------------------------------------------------------------------- #
# history cursor
# --------------------------------------------------------------------------- #
def _load_history():
    try:
        with open(HISTORY_FILE) as fh:
            return str(json.load(fh)["historyId"])
    except Exception:
        return None


def _save_history(history_id):
    with open(HISTORY_FILE, "w") as fh:
        json.dump({"historyId": str(history_id)}, fh)


# --------------------------------------------------------------------------- #
# message parsing
# --------------------------------------------------------------------------- #
def _header(headers, name):
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _walk(payload):
    """Return (text, html, [attachment names])."""
    text, html, atts = "", "", []
    stack = [payload]
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if part.get("filename"):
            atts.append(part["filename"])
        if data:
            decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            if mime == "text/plain":
                text += decoded
            elif mime == "text/html":
                html += decoded
        stack.extend(part.get("parts") or [])
    return text, html, atts


def _normalize(msg):
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    text, html, atts = _walk(payload)

    def addrs(name):
        raw = _header(headers, name)
        return [a.strip() for a in raw.split(",")] if raw else []

    return {
        "message_id":  _header(headers, "Message-ID") or msg.get("id"),
        "thread_id":   msg.get("threadId"),
        "in_reply_to": _header(headers, "In-Reply-To"),
        "gmail_id":    msg.get("id"),
        "date":        _header(headers, "Date"),
        "from":        addrs("From"),
        "to":          addrs("To"),
        "cc":          addrs("Cc"),
        "bcc":         addrs("Bcc"),
        "reply_to":    addrs("Reply-To"),
        "subject":     _header(headers, "Subject"),
        "labels":      as_list(msg.get("labelIds")),
        "text":        text,
        "html":        html,
        "attachments": atts,
    }


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #
def process_history(service, start_history_id):
    seen = set()
    page_token = None
    latest = start_history_id
    while True:
        resp = service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            pageToken=page_token,
        ).execute()
        latest = resp.get("historyId", latest)

        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_stub = added.get("message", {})
                mid = msg_stub.get("id")
                labels = msg_stub.get("labelIds") or []
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                if "INBOX" not in labels or "UNREAD" not in labels:
                    continue

                full = service.users().messages().get(
                    userId="me", id=mid, format="full").execute()
                norm = _normalize(full)

                try:
                    service.users().messages().modify(
                        userId="me", id=mid,
                        body={"removeLabelIds": ["UNREAD"]}).execute()
                    norm["marked_read"] = True
                except Exception as exc:
                    print(f"[gmail] mark-read failed: {exc}")
                    norm["marked_read"] = False

                print_message("gmail", norm)

                try:
                    from pipeline import process as _run_pipeline
                    _run_pipeline(norm, "gmail", USER)
                except Exception as exc:  # noqa: BLE001
                    print(f"[gmail] pipeline error: {exc}")

        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return latest


# --------------------------------------------------------------------------- #
# outbound (used by mailer.py)
# --------------------------------------------------------------------------- #
def _raw(to, subject, text, cc=None, bcc=None, headers=None):
    from email.message import EmailMessage
    from email.utils import formataddr

    m = EmailMessage()
    if FROM_NAME:
        m["From"] = formataddr((FROM_NAME, USER))
    m["To"] = to
    m["Subject"] = subject
    if cc:
        m["Cc"] = ", ".join(cc)
    if bcc:
        m["Bcc"] = ", ".join(bcc)
    for k, v in (headers or {}).items():
        if v:
            m[k] = v
    m.set_content(text)
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


def send_mail(*, to, subject, text, cc=None, bcc=None):
    sent = service_send({"raw": _raw(to, subject, text, cc, bcc)})
    return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}


def reply_mail(original, *, text, subject=None, cc=None, bcc=None):
    to = (original.get("from") or [None])[0]
    subj = subject or f"Re: {original.get('subject') or ''}".strip()
    ref = original.get("message_id")
    body = {"raw": _raw(to, subj, text, cc, bcc,
                        headers={"In-Reply-To": ref, "References": ref})}
    if original.get("thread_id"):
        body["threadId"] = original["thread_id"]
    sent = service_send(body)
    return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}


def service_send(body):
    return get_service().users().messages().send(userId="me", body=body).execute()


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.post("/webhook")
def push():
    if _PUSH_TOKEN_CONFIGURED and request.args.get("token") != PUSH_TOKEN:
        return "forbidden", 403

    envelope = request.get_json(force=True, silent=True) or {}
    data = (envelope.get("message") or {}).get("data")
    if not data:
        return "", 204                      # ack malformed / empty push

    decoded = json.loads(base64.b64decode(data).decode("utf-8"))
    # decoded == {"emailAddress": "...", "historyId": 123456}
    start = _load_history() or str(decoded["historyId"])

    try:
        service = get_service()
        latest = process_history(service, start)
        _save_history(latest)
    except Exception as exc:
        print(f"[gmail] processing error: {exc}")
        return "", 500                       # let Pub/Sub retry
    return "", 204


@app.get("/health")
def health():
    return jsonify(ok=True, provider="gmail", user=USER)


# --------------------------------------------------------------------------- #
# admin ops  (CLI + HTTP /admin/gmail/<action> via main.py)
# --------------------------------------------------------------------------- #
def start_watch() -> dict:
    if not PUBSUB_TOPIC:
        raise RuntimeError('GOOGLE_MAIL["pubsub_topic"] is not set')
    resp = get_service().users().watch(userId="me", body={
        "topicName": PUBSUB_TOPIC,
        "labelIds": ["INBOX"],
        "labelFilterBehavior": "INCLUDE",
    }).execute()
    try:
        _save_history(resp["historyId"])
    except Exception as exc:  # noqa: BLE001 - ephemeral disk on Cloud Run
        print(f"[gmail] could not persist historyId: {exc}")
    return {"provider": "gmail", "action": "watch", **resp}


def stop_watch() -> dict:
    get_service().users().stop(userId="me").execute()
    return {"provider": "gmail", "action": "stop", "ok": True}


def status() -> dict:
    p = get_service().users().getProfile(userId="me").execute()
    return {"provider": "gmail",
            "emailAddress": p.get("emailAddress"),
            "messagesTotal": p.get("messagesTotal"),
            "historyId": p.get("historyId")}


def admin_op(action: str) -> dict:
    ops = {"setup": start_watch, "renew": start_watch,
           "teardown": stop_watch, "status": status}
    if action not in ops:
        raise ValueError(f"gmail: unknown admin action {action!r}")
    return ops[action]()


def cmd_check():
    print(json.dumps(status(), indent=2))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    actions = {"check": cmd_check,
               "setup": lambda: print(json.dumps(start_watch(), indent=2)),
               "stop": lambda: print(json.dumps(stop_watch(), indent=2))}
    if cmd in actions:
        actions[cmd]()
    else:
        app.run(host=env("HOST", "0.0.0.0"), port=int(env("PORT", "8080")))


if __name__ == "__main__":
    main()
