"""
Microsoft 365 / Outlook  ->  coordinate-inbox listener
======================================================

Delivery : Microsoft Graph change-notification subscription
           (changeType = "created" on the Inbox folder)
Flow     : Graph validation handshake  -> echo validationToken
           notification (resourceData.id)
           -> GET /users/<mailbox>/messages/<id>
           -> print every field (from / to / cc / bcc / subject / body / ...)
           -> PATCH /users/<mailbox>/messages/<id>  {"isRead": true}

Auth     : app-only (client credentials).  Grant the app registration the
           APPLICATION permission  Mail.ReadWrite  and grant admin consent.

Commands  (also POST /admin/m365/<setup|renew|teardown|status> via main.py)
--------
    python providers/m365.py setup      create the subscription (idempotent)
    python providers/m365.py renew      extend the subscription expiry
    python providers/m365.py delete     delete the subscription
    python providers/m365.py status     list this app's Graph subscriptions
    python providers/m365.py serve      run the notification receiver   (default)

The subscription id is discovered from Graph (GET /subscriptions matched on
notificationUrl), so renew/delete are stateless - no local file needed.

Config : everything is ONE JSON secret, MS365_Mail (config.py / BUNDLED_SECRETS);
         config.get("MS_...") reads the matching field:
    MS_TENANT_ID
    MS_CLIENT_ID
    MS_CLIENT_SECRET
    MS_MAILBOX              mailbox address        (falls back to COORDINATE_EMAIL)
    MS_NOTIFICATION_URL     public https URL ending in /webhook     (setup only)
    MS_CLIENT_STATE         random string echoed back in every notification
The subscription id is cached in a fixed local file, m365_subscription.json.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Run either as `python providers/m365.py` or `from providers.m365 import app`.
# Drop this dir from sys.path (so the filename can't shadow a package) and add
# the repo root so `import common` works.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
sys.path.insert(0, os.path.dirname(_HERE))

import msal
import requests
from flask import Flask, request, jsonify, Response

from common import env, as_list, print_message  # env(): env var -> Secret Manager
from config import config

# all MS_* keys are fields of the MS365_Mail JSON secret (see config.BUNDLED_SECRETS)
TENANT        = env("MS_TENANT_ID", required=True)
CLIENT_ID     = env("MS_CLIENT_ID", required=True)
CLIENT_SECRET = env("MS_CLIENT_SECRET", required=True)
MAILBOX       = env("MS_MAILBOX") or env("COORDINATE_EMAIL", required=True)
NOTIFY_URL    = env("MS_NOTIFICATION_URL")
CLIENT_STATE  = env("MS_CLIENT_STATE", "change-me")

# sender display name shown to recipients, e.g. "Zee-Coordinator" instead of the
# mailbox's directory display name - MS365_Mail["FROM_NAME"]. Read straight off the
# whole JSON blob (not through BUNDLED_SECRETS) since "FROM_NAME" is also used inside
# AGENTMAIL_CONFIG / GOOGLE_MAIL and each provider's value must stay independent.
FROM_NAME = ((config.get_json("MS365_Mail") or {}).get("FROM_NAME") or "").strip() or None

# subscription-id cache - fixed local file, no env var
SUB_FILE      = "m365_subscription.json"

GRAPH = "https://graph.microsoft.com/v1.0"
app = Flask(__name__)


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def _token():
    cca = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        client_credential=CLIENT_SECRET,
    )
    result = cca.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", str(result)))
    return result["access_token"]


def _headers():
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


# --------------------------------------------------------------------------- #
# message parsing
# --------------------------------------------------------------------------- #
def _emails(recipients):
    out = []
    for r in recipients or []:
        if not r:
            continue
        addr = (r.get("emailAddress") or {})
        out.append(addr.get("address") or addr.get("name"))
    return [x for x in out if x]


def _normalize(m):
    body = m.get("body", {}) or {}
    is_html = body.get("contentType", "").lower() == "html"
    hdrs = {h["name"].lower(): h["value"]
            for h in (m.get("internetMessageHeaders") or [])}
    bcc = _emails(m.get("bccRecipients"))
    if not bcc and "bcc" in hdrs:
        bcc = [hdrs["bcc"]]
    return {
        "message_id":  m.get("internetMessageId") or m.get("id"),
        "thread_id":   m.get("conversationId"),
        "in_reply_to": hdrs.get("in-reply-to"),
        "graph_id":    m.get("id"),
        "date":        m.get("receivedDateTime"),
        "from":        _emails([m.get("from")]) or _emails([m.get("sender")]),
        "to":          _emails(m.get("toRecipients")),
        "cc":          _emails(m.get("ccRecipients")),
        "bcc":         bcc,
        "reply_to":    _emails(m.get("replyTo")),
        "subject":     m.get("subject"),
        "labels":      (["read"] if m.get("isRead") else ["unread"]) + as_list(m.get("categories")),
        "text":        "" if is_html else body.get("content", ""),
        "html":        body.get("content", "") if is_html else "",
        "attachments": [],
    }


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #
def handle(message_id):
    select = ("id,internetMessageId,receivedDateTime,from,sender,toRecipients,"
              "ccRecipients,bccRecipients,replyTo,subject,body,isRead,categories,hasAttachments")
    url = (f"{GRAPH}/users/{MAILBOX}/messages/{message_id}"
           f"?$select={select}&$expand=internetMessageHeaders")

    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        print(f"[m365] fetch failed {resp.status_code}: {resp.text[:300]}")
        return
    m = resp.json()
    norm = _normalize(m)

    if m.get("hasAttachments"):
        ar = requests.get(
            f"{GRAPH}/users/{MAILBOX}/messages/{message_id}/attachments?$select=name",
            headers=_headers(), timeout=30)
        if ar.ok:
            norm["attachments"] = [a.get("name") for a in ar.json().get("value", [])]

    pr = requests.patch(
        f"{GRAPH}/users/{MAILBOX}/messages/{message_id}",
        headers=_headers(), json={"isRead": True}, timeout=30)
    norm["marked_read"] = pr.status_code in (200, 204)
    if not norm["marked_read"]:
        print(f"[m365] mark-read failed {pr.status_code}: {pr.text[:200]}")

    print_message("m365", norm)

    try:
        from pipeline import process as _run_pipeline
        _run_pipeline(norm, "m365", MAILBOX)
    except Exception as exc:  # noqa: BLE001
        print(f"[m365] pipeline error: {exc}")


# --------------------------------------------------------------------------- #
# outbound (used by mailer.py)
# --------------------------------------------------------------------------- #
def _recips(addresses):
    return [{"emailAddress": {"address": a}} for a in (addresses or []) if a]


def _from_field():
    """Graph 'from' object for FROM_NAME, or None if not configured.
    Note: Exchange/Graph may still enforce the mailbox's directory display name
    depending on tenant policy and Send-As rights - this sets it, but the tenant
    has the final say."""
    if not FROM_NAME:
        return None
    return {"emailAddress": {"name": FROM_NAME, "address": MAILBOX}}


def send_mail(*, to, subject, text, cc=None, bcc=None):
    message = {
        "subject": subject,
        "body": {"contentType": "Text", "content": text},
        "toRecipients": _recips([to]),
        "ccRecipients": _recips(cc),
        "bccRecipients": _recips(bcc),
    }
    from_field = _from_field()
    if from_field:
        message["from"] = from_field
    r = requests.post(f"{GRAPH}/users/{MAILBOX}/sendMail",
                      headers=_headers(), json={"message": message, "saveToSentItems": True},
                      timeout=30)
    if r.status_code not in (200, 202):
        print(f"[m365] sendMail failed {r.status_code}: {r.text[:200]}")
    # Graph sendMail returns 202 with no body / id.
    return {"message_id": None, "thread_id": None, "status": r.status_code}


def reply_mail(original, *, text, subject=None, cc=None, bcc=None):
    gid = original.get("graph_id")
    if gid:
        if cc or bcc or FROM_NAME:
            # Graph's /reply action can't add recipients or a custom From name in one
            # call - build the reply as a draft, patch it, then send that draft.
            cr = requests.post(f"{GRAPH}/users/{MAILBOX}/messages/{gid}/createReply",
                               headers=_headers(), timeout=30)
            if cr.status_code not in (200, 201):
                print(f"[m365] createReply failed {cr.status_code}: {cr.text[:200]}")
                return {"message_id": None, "thread_id": original.get("thread_id"),
                        "status": cr.status_code}
            draft_id = cr.json().get("id")
            patch = {"body": {"contentType": "Text", "content": text},
                     "ccRecipients": _recips(cc), "bccRecipients": _recips(bcc)}
            from_field = _from_field()
            if from_field:
                patch["from"] = from_field
            pr = requests.patch(f"{GRAPH}/users/{MAILBOX}/messages/{draft_id}",
                                headers=_headers(), json=patch, timeout=30)
            if pr.status_code not in (200, 201):
                print(f"[m365] reply draft update failed {pr.status_code}: {pr.text[:200]}")
            sr = requests.post(f"{GRAPH}/users/{MAILBOX}/messages/{draft_id}/send",
                               headers=_headers(), timeout=30)
            if sr.status_code not in (200, 202):
                print(f"[m365] reply send failed {sr.status_code}: {sr.text[:200]}")
            return {"message_id": draft_id, "thread_id": original.get("thread_id"),
                    "status": sr.status_code}
        r = requests.post(f"{GRAPH}/users/{MAILBOX}/messages/{gid}/reply",
                          headers=_headers(), json={"comment": text}, timeout=30)
        if r.status_code not in (200, 202):
            print(f"[m365] reply failed {r.status_code}: {r.text[:200]}")
        return {"message_id": None, "thread_id": original.get("thread_id"),
                "status": r.status_code}
    to = (original.get("from") or [None])[0]
    return send_mail(to=to, subject=subject or f"Re: {original.get('subject') or ''}".strip(),
                     text=text, cc=cc, bcc=bcc)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    # Graph subscription validation handshake
    token = request.args.get("validationToken")
    if token:
        return Response(token, mimetype="text/plain")

    payload = request.get_json(force=True, silent=True) or {}
    for note in payload.get("value", []):
        if note.get("clientState") != CLIENT_STATE:
            print("[m365] clientState mismatch; ignoring notification")
            continue
        mid = (note.get("resourceData") or {}).get("id")
        if not mid:
            continue
        try:
            handle(mid)
        except Exception as exc:
            print(f"[m365] handler error: {exc}")
    return "", 202


@app.get("/health")
def health():
    return jsonify(ok=True, provider="m365", mailbox=MAILBOX)


# --------------------------------------------------------------------------- #
# admin ops  (CLI + HTTP /admin/m365/<action> via main.py)
# --------------------------------------------------------------------------- #
def _expiry():
    # Graph max for message resources is ~4230 minutes (< 3 days)
    return (datetime.now(timezone.utc) + timedelta(minutes=4230)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _try_save_sub(data):
    try:
        with open(SUB_FILE, "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception:  # noqa: BLE001 - ephemeral disk on Cloud Run
        pass


def _try_load_sub():
    try:
        with open(SUB_FILE) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def _find_subscription():
    """Locate this app's Inbox subscription from Graph (stateless - survives restarts)."""
    r = requests.get(f"{GRAPH}/subscriptions", headers=_headers(), timeout=30)
    r.raise_for_status()
    subs = r.json().get("value", [])
    mbx = (MAILBOX or "").lower()
    for s in subs:
        if s.get("notificationUrl") == NOTIFY_URL and mbx in (s.get("resource", "").lower()):
            return s
    for s in subs:
        if s.get("notificationUrl") == NOTIFY_URL:
            return s
    return None


def create_subscription() -> dict:
    if not NOTIFY_URL:
        raise RuntimeError("MS_NOTIFICATION_URL is not set")
    existing = _find_subscription()
    if existing:
        return renew_subscription(existing)
    body = {
        "changeType": "created",
        "notificationUrl": NOTIFY_URL,
        "resource": f"users/{MAILBOX}/mailFolders('Inbox')/messages",
        "expirationDateTime": _expiry(),
        "clientState": CLIENT_STATE,
    }
    r = requests.post(f"{GRAPH}/subscriptions", headers=_headers(), json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    _try_save_sub(data)
    return {"provider": "m365", "action": "create", "subscription": data}


def renew_subscription(sub=None) -> dict:
    sub = sub or _find_subscription() or _try_load_sub()
    if not sub:
        return create_subscription()
    r = requests.patch(f"{GRAPH}/subscriptions/{sub['id']}", headers=_headers(),
                       json={"expirationDateTime": _expiry()}, timeout=30)
    r.raise_for_status()
    data = r.json()
    _try_save_sub(data)
    return {"provider": "m365", "action": "renew",
            "id": data.get("id"), "expirationDateTime": data.get("expirationDateTime")}


def delete_subscription() -> dict:
    sub = _find_subscription() or _try_load_sub()
    if not sub:
        return {"provider": "m365", "action": "delete", "note": "no subscription found"}
    r = requests.delete(f"{GRAPH}/subscriptions/{sub['id']}", headers=_headers(), timeout=30)
    return {"provider": "m365", "action": "delete", "id": sub.get("id"), "status": r.status_code}


def status() -> dict:
    r = requests.get(f"{GRAPH}/subscriptions", headers=_headers(), timeout=30)
    r.raise_for_status()
    return {"provider": "m365", "subscriptions": r.json().get("value", [])}


def admin_op(action: str) -> dict:
    ops = {"setup": create_subscription, "renew": renew_subscription,
           "teardown": delete_subscription, "status": status}
    if action not in ops:
        raise ValueError(f"m365: unknown admin action {action!r}")
    return ops[action]()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    actions = {
        "setup": lambda: print(json.dumps(create_subscription(), indent=2)),
        "renew": lambda: print(json.dumps(renew_subscription(), indent=2)),
        "delete": lambda: print(json.dumps(delete_subscription(), indent=2)),
        "status": lambda: print(json.dumps(status(), indent=2)),
    }
    if cmd in actions:
        actions[cmd]()
    else:
        app.run(host=env("HOST", "0.0.0.0"), port=int(env("PORT", "8080")))


if __name__ == "__main__":
    main()
