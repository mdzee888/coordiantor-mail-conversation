# Coordinate-inbox listener

One "coordinate" mailbox, served by **AgentMail**, **Gmail / Google Workspace**,
**Microsoft 365**, or any mix of them. Whenever a mail lands in that inbox the
matching script:

> This file covers the inbox connection layer (providers, webhooks, deployment).
> For what actually happens to a mail once it arrives — whitelist checks, campaign
> creation, AI classification, supplier replies — see **[PIPELINE.md](PIPELINE.md)**.

1. receives the provider's push / webhook notification,
2. fetches the **full** message from the provider API,
3. prints every field — `from`, `to`, `cc`, `bcc`, `reply-to`, `subject`,
   `labels`, attachment names, body,
4. **marks the mail as read**.

| file                     | provider      | delivery mechanism                         | mark-as-read                         |
|--------------------------|---------------|-------------------------------------------|--------------------------------------|
| `providers/agentmail.py` | AgentMail     | webhook, event `message.received`         | label `unread` -> `read`             |
| `providers/gmail.py`     | Gmail         | `users.watch()` -> Cloud Pub/Sub push     | remove `UNREAD` label (`messages.modify`) |
| `providers/m365.py`      | Microsoft 365 | Graph change-notification subscription    | `PATCH … {"isRead": true}`           |
| `main.py`                | —             | runs the active ones together on one port | —                                    |
| `common.py`              | —             | shared normalize + pretty-print           | —                                    |

## Configuration

Every setting is read through **`config.py`**, which resolves each key as:

1. an OS environment variable of that name, then
2. a field inside a shared JSON secret, for bundled keys —
   **`DB_*` → `COORDINATOR_DB_CONNECTION`**, **`AGENTMAIL_*` → `AGENTMAIL_CONFIG`**,
   **`MS_*` → `MS365_Mail`** (each a JSON object), then
3. a **GCP Secret Manager** secret of the same name (secret id == key name), then
4. the code default.

In production only `GCP_PROJECT_ID` and `GCS_BUCKET_NAME` are real env vars
(injected by `cloudbuild.yaml`); everything else lives in Secret Manager, one
secret per key. Locally, `config.py` also `load_dotenv()`s a `.env` file so you
can run without GCP. `secret_manager.py` (the Secret Manager client) is used
as-is. `config.ALL_KEYS` lists every catalogued key.

In code, use `config` instead of `os.getenv`:

```python
from config import config
config.get("AGENTMAIL_API_KEY")        # str | None  (field of AGENTMAIL_CONFIG)
config.get_int("PORT", 8080)
config.get_bool("DB_ECHO", False)
config.require("DB_PASSWORD")           # raises if unset
config.get_json("GOOGLE_MAIL")          # dict | None  (whole JSON credential secret)
```

Structured credential blobs whose fields aren't flat keys — **`GOOGLE_MAIL`**
(`coordinator_mail` / `client_id` / `client_secret` / `refresh_token` /
`token_uri`) — are read whole with `config.get_json()` / `config.require_json()`.

### Database

`COORDINATOR_DB_CONNECTION` (Cloud SQL for PostgreSQL) is one more bundled JSON
secret, read the same way — `config.get("DB_HOST")` etc. pull a field out of it:
```json
{
  "DB_HOST": "127.0.0.1", "DB_PORT": 5432, "DB_NAME": "...", "DB_USER": "...",
  "DB_PASSWORD": "...", "DB_SSLMODE": "",
  "DATABASE_URL": "",
  "DB_POOL_SIZE": 5, "DB_MAX_OVERFLOW": 10, "DB_POOL_RECYCLE": 1800, "DB_ECHO": false
}
```
`DATABASE_URL`, if set, overrides every discrete `DB_*` field. See
[db/dbmodel.py](db/dbmodel.py) for the full schema (five tables) and
[PIPELINE.md](PIPELINE.md) for what actually reads/writes it.

### LLM model profile

Every AI agent shares one model-profile JSON secret, named by `LLM_PROFILE`
(default `"GEMINI_LITE"`):
```json
{"MODEL": "gemini/gemini-3.1-flash-lite", "TEMPERATURE": 0, "MAX_TOKENS": 8192,
 "GEMINI_API_KEY": "..."}
```
Per-agent overrides win over the shared profile: `<AGENT>_MODEL` / `_PROVIDER` /
`_TEMPERATURE` / `_MAX_TOKENS` / `_API_KEY` / `_API_BASE`, where `<AGENT>` is the
agent's name upper-cased (`CAMPAIGN_CREATE_MODEL`, `SUPPLIER_OOO_CHECK_MODEL`, …).
See [PIPELINE.md → The AI agents](PIPELINE.md#5-the-ai-agents) for what each one does.

## Setup

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt      # Windows
cp .env.example .env                               # local dev only; prod uses Secret Manager
```

`ACTIVE_PROVIDERS=auto` makes `main.py` start only the providers whose
keys resolve. Each script also runs on its own.

The webhook receivers must be reachable on a **public HTTPS URL** (use a tunnel
such as `cloudflared` / `ngrok` during development).

### AgentMail

An optional `"FROM_NAME"` field in `AGENTMAIL_CONFIG` (alongside `AGENTMAIL_API_KEY`
etc.) sets the sender display name recipients see instead of the inbox's own name -
AgentMail has no per-message "from name", so this is synced onto the **inbox's**
`display_name` by `setup` / `renew` (`sync_display_name()` in
[providers/agentmail.py](providers/agentmail.py)), not sent per email.

```bash
python providers/agentmail.py setup      # registers AGENTMAIL_WEBHOOK_URL, prints whsec_… secret, syncs FROM_NAME
# put the secret in AGENTMAIL_WEBHOOK_SECRET, then:
python providers/agentmail.py serve
python providers/agentmail.py backfill    # optional: process existing unread mail
```

### Gmail / Google Workspace

Everything is in the one `GOOGLE_MAIL` JSON secret — no client-secret file, no
local consent, no separate Pub/Sub env vars:
`{"coordinator_mail","client_id","client_secret","refresh_token","token_uri","pubsub_topic","push_token","FROM_NAME"}`.
`push_token` is optional (a per-process one is generated if omitted, but then the
push-URL check isn't enforced); the history cursor is a fixed local file.
`FROM_NAME` is optional - when set, every send/reply carries it as the sender
display name (e.g. `"Zee-Coordinator" <coordinator_mail>`) instead of the Google
account's own name.

Prereqs in Google Cloud: enable the Gmail API, create the Pub/Sub topic named in
`pubsub_topic`, grant `gmail-api-push@system.gserviceaccount.com` the **Pub/Sub
Publisher** role on it, and create a **push** subscription pointing at
`https://<host>/gmail/webhook?token=<push_token>`.

```bash
python providers/gmail.py check      # connect with GOOGLE_MAIL creds, print the profile
python providers/gmail.py setup      # users.watch(); re-run at least weekly (7-day cap)
python providers/gmail.py serve
python providers/gmail.py stop       # stop notifications
```

### Microsoft 365

Prereqs in Entra ID: an app registration with the **application** permission
`Mail.ReadWrite` (admin-consented) and a client secret. Put everything in the
`MS365_Mail` JSON secret:
`{"MS_TENANT_ID","MS_CLIENT_ID","MS_CLIENT_SECRET","MS_MAILBOX","MS_NOTIFICATION_URL","MS_CLIENT_STATE","FROM_NAME"}`.
The subscription id is cached in a fixed local file. `FROM_NAME` is optional and
sets the sender display name on every send/reply - note Exchange/Graph tenant
policy has the final say on whether a custom From name is honored (it depends on
Send-As configuration), unlike AgentMail/Gmail where it's unconditional.

```bash
python providers/m365.py setup       # creates the subscription (validation handshake happens here)
python providers/m365.py serve
python providers/m365.py renew        # extend expiry (< 3 days); run on a schedule
python providers/m365.py delete
```

The M365 subscription id is looked up from Graph (`GET /subscriptions` matched on
`notificationUrl`), so `renew` / `delete` work on a fresh instance with no local
state file.

### All together

```bash
python main.py                       # local dev (werkzeug)
gunicorn main:application             # production
```

Inbound webhook URLs:

```
https://<host>/agentmail/webhook
https://<host>/gmail/webhook?token=<GOOGLE_MAIL.push_token>
https://<host>/m365/webhook
```

## Deploy to Cloud Run

`Dockerfile` runs `gunicorn main:application` (WSGI). `cloudbuild.yaml` builds,
pushes, and deploys.

Prerequisites:

1. Secrets in Secret Manager (project `GCP_PROJECT_ID`):
   `GOOGLE_MAIL`, `AGENTMAIL_CONFIG`, `MS365_Mail`, `COORDINATOR_DB_CONNECTION`,
   `GEMINI_LITE`, `ADMIN_TOKEN` (create whichever providers you use). Add
   `"FROM_NAME"` inside each provider's own secret if you want a custom sender
   display name — see each provider's section above.
2. The Cloud Run **runtime service account** has
   `roles/secretmanager.secretAccessor` on the project.
3. `pubsub_topic` / `notificationUrl` inside the secrets point at
   `https://<service-url>/gmail/webhook` and `https://<service-url>/m365/webhook`.
4. If `COORDINATOR_DB_CONNECTION` points at a Cloud SQL instance on a **private
   IP**, Cloud Run needs a route into that VPC — `cloudbuild.yaml` deploys with
   `--vpc-connector <name>`, which must already exist:
   ```bash
   gcloud compute networks vpc-access connectors create <name> \
     --region=<region> --network=<vpc> --range=10.8.0.0/28
   ```
   (Skip this if `DATABASE_URL`/`DB_HOST` instead points at a publicly reachable
   database, or a Cloud SQL instance reached over its public IP.)

### Register the webhooks *after* deploy (no CLI)

The service exposes an admin API. Auth header: `X-Admin-Token: <ADMIN_TOKEN>`
(`Authorization: Bearer <ADMIN_TOKEN>` also works). With `ADMIN_TOKEN` unset the
admin API returns 503.

| Method & path | Does |
|---|---|
| `POST /admin/setup` | create every active provider's webhook / subscription |
| `POST /admin/renew` | renew every active provider (Gmail watch, M365 subscription) |
| `POST /admin/teardown` | remove them |
| `POST /admin/status` | report each provider's hook state |
| `POST /admin/<provider>/<action>` | same, one provider (`agentmail`/`gmail`/`m365`) |

```bash
TOKEN=…            # the ADMIN_TOKEN secret value
URL=https://<service-url>
curl -XPOST -H "X-Admin-Token: $TOKEN" $URL/admin/setup
```

### Keep them alive with Cloud Scheduler

Gmail `watch` expires after **7 days**, the M365 subscription after **~3 days**.
One scheduler job on `/admin/renew` covers both:

```bash
gcloud scheduler jobs create http coordinator-renew \
  --schedule="0 */12 * * *" \
  --uri="https://<service-url>/admin/renew" --http-method=POST \
  --headers="X-Admin-Token=<ADMIN_TOKEN>" \
  --location=us-central1
```

For a private service, also pass `--oidc-service-account-email=<sa>` and deploy
with `--no-allow-unauthenticated`: Cloud Run then checks the OIDC `Authorization`
header and the app still checks `X-Admin-Token` — the two don't collide.

## Notes

- **BCC**: providers only expose BCC recipients that the *server copy* of the
  message actually carries — for normal inbound mail that list is usually empty
  for everyone except the BCC'd recipient themselves. The scripts print whatever
  the API returns (M365 also falls back to a `Bcc:` header if present). This is
  also why a campaign's `bcc_emails` (see
  [PIPELINE.md → Cc / Bcc](PIPELINE.md#7b-cc--bcc-on-every-outbound-mail)) will
  almost always come back empty — the coordinator was never shown the OEM's
  original Bcc list to begin with.
- Gmail `watch` expires after 7 days; the M365 subscription after ~3 days. Keep
  `setup` / `renew` on a cron.
- Secrets and generated state files (`.env`, `gmail_token.json`,
  `m365_subscription.json`, …) are git-ignored.
