"""
config.py — single source of truth for every configuration key in this service.

WHY
---
In production the real values are **not** shipped as environment variables. They
live in **GCP Secret Manager**. Most keys are their own secret (secret id ==
key name). Related keys are grouped into a single JSON secret instead - see
BUNDLED_SECRETS (DB_* -> COORDINATOR_DB_CONNECTION, AGENTMAIL_* -> AGENTMAIL_CONFIG).

The only values delivered as real environment variables are the ones set in
`cloudbuild.yaml` (`--set-env-vars`): `GCP_PROJECT_ID`, `GCS_BUCKET_NAME`.

RESOLUTION ORDER  (per key, re-fetched from Secret Manager on every call - no caching)
---------------------------------------------------------------------------------
    1. os.environ[KEY]                    - cloudbuild --set-env-vars / local shell / .env
    2. a field inside a shared JSON secret, for keys listed in BUNDLED_SECRETS:
         DB_*        -> secret  COORDINATOR_DB_CONNECTION
         AGENTMAIL_* -> secret  AGENTMAIL_CONFIG
         MS_*        -> secret  MS365_Mail
    3. Secret Manager secret "KEY"        - via secret_manager.SecretManager (used as-is)
    4. the default you pass to get()/get_int()/get_bool(), else None

USAGE  — replace every `os.getenv("X")` / `os.environ["X"]` / `load_dotenv()` with:
--------------------------------------------------------------------------------
    from config import config

    config.get("AGENTMAIL_API_KEY")                # -> str | None
    config.get("HOST", "0.0.0.0")                  # -> str with default
    config.get_int("PORT", 8080)                   # -> int
    config.get_bool("DB_ECHO", False)              # -> bool
    config.require("DB_PASSWORD")                   # -> str, raises if absent
    config.get_json("GOOGLE_MAIL")                  # -> dict | None (whole JSON secret)
    config.require_json("GOOGLE_MAIL")              # -> dict, raises if absent/invalid

Keys are looked up by their plain string name. Most bundled keys live inside a
JSON secret (all DB_*, AGENTMAIL_* and MS_*) - see BUNDLED_SECRETS. Structured
credential blobs whose fields are not flat keys (GOOGLE_MAIL, the LLM profile)
are read whole with get_json(). A few plain settings with defaults
(COORDINATE_EMAIL, ACTIVE_PROVIDERS, HOST, PORT, LOG_LEVEL) need no secret.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Optional

# Optional: let local development keep using a .env file. In production this is a
# no-op (python-dotenv may not even be installed) and Secret Manager is used.
try:  # pragma: no cover
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

if TYPE_CHECKING:  # import kept lazy so local dev works without the GCP lib
    from secret_manager import SecretManager

# ===========================================================================
#  Environment-only keys (delivered by cloudbuild.yaml --set-env-vars)
# ===========================================================================
GCP_PROJECT_ID = "GCP_PROJECT_ID"
GCS_BUCKET_NAME = "GCS_BUCKET_NAME"

# ===========================================================================
#  Every configuration key used anywhere in the codebase.
#  Secret Manager secret id == the string value.
# ===========================================================================

# --- Coordinate mailbox / HTTP server --------------------------------------
# Not required in production - the code reads these by string literal and every
# one has a working default (or is injected by the platform):
#   COORDINATE_EMAIL  fallback mailbox; unused once each provider's own mailbox
#                     is set (AGENTMAIL_CONFIG / MS365_Mail / GOOGLE_MAIL)
#   ACTIVE_PROVIDERS  default "auto" (detect from which secrets resolve)
#   HOST              default "0.0.0.0"
#   PORT              default 8080 (Cloud Run injects PORT automatically)
#   LOG_LEVEL         default "INFO"
# COORDINATE_EMAIL = "COORDINATE_EMAIL"
# ACTIVE_PROVIDERS = "ACTIVE_PROVIDERS"
# HOST = "HOST"
# PORT = "PORT"
# LOG_LEVEL = "LOG_LEVEL"

# Bearer token that guards the /admin/* API (Cloud Scheduler -> webhook setup).
# When unset the admin API returns 503 (disabled).
ADMIN_TOKEN = "ADMIN_TOKEN"

# --- AgentMail ------------------------------------------------------------
# Not stored as individual secrets. All AgentMail values live inside ONE Secret
# Manager secret, AGENTMAIL_CONFIG, whose value is a JSON object:
#   {"AGENTMAIL_API_KEY": "...", "AGENTMAIL_INBOX_ID": "...",
#    "AGENTMAIL_WEBHOOK_URL": "...", "AGENTMAIL_WEBHOOK_SECRET": ""}
# config.get("AGENTMAIL_API_KEY") pulls the field out; an env var still overrides.
AGENTMAIL_CONFIG = "AGENTMAIL_CONFIG"
AGENTMAIL_CONFIG_FIELDS = (
    "AGENTMAIL_API_KEY",
    "AGENTMAIL_INBOX_ID",
    "AGENTMAIL_WEBHOOK_URL",
    "AGENTMAIL_WEBHOOK_SECRET",
)

# --- Gmail / Google Workspace -------------------------------------------
# Everything Gmail needs lives in ONE Secret Manager secret, GOOGLE_MAIL, a JSON
# object (read via config.get_json("GOOGLE_MAIL")):
#   {
#     "coordinator_mail": "coordinator@example.com",
#     "client_id":     "5452...apps.googleusercontent.com",
#     "client_secret": "GOCSPX-....",
#     "refresh_token": "1//04....",
#     "token_uri":     "https://oauth2.googleapis.com/token",
#     "pubsub_topic":  "projects/<proj>/topics/gmail-inbox",
#     "push_token":    "<optional shared secret for the push URL>"
#   }
GOOGLE_MAIL = "GOOGLE_MAIL"
GOOGLE_MAIL_FIELDS = (
    "coordinator_mail",
    "client_id",
    "client_secret",
    "refresh_token",
    "token_uri",
    "pubsub_topic",     # required for `gmail.py setup` (users.watch)
    "push_token",        # optional - gmail.py generates a fallback if absent
)

# --- Microsoft 365 / Outlook ------------------------------------------
# Not stored as individual secrets. All values live inside ONE Secret Manager
# secret, MS365_Mail, whose value is a JSON object:
#   {"MS_TENANT_ID": "...", "MS_CLIENT_ID": "...", "MS_CLIENT_SECRET": "...",
#    "MS_MAILBOX": "coordinator@example.com",
#    "MS_NOTIFICATION_URL": "https://your-domain.com/m365/webhook",
#    "MS_CLIENT_STATE": "your-random-secret"}
# config.get("MS_TENANT_ID") pulls the field out; an env var still overrides.
# (The subscription-id cache is a fixed local file - m365_subscription.json.)
MS365_MAIL = "MS365_Mail"
MS365_MAIL_FIELDS = (
    "MS_TENANT_ID",
    "MS_CLIENT_ID",
    "MS_CLIENT_SECRET",
    "MS_MAILBOX",
    "MS_NOTIFICATION_URL",
    "MS_CLIENT_STATE",
)

# --- Database (GCP Cloud SQL for PostgreSQL) ---------------------------
# Not stored as individual secrets. All DB values live inside ONE Secret Manager
# secret, COORDINATOR_DB_CONNECTION, whose value is a JSON object:
#   {"DB_HOST": "127.0.0.1", "DB_PORT": 5432, "DB_NAME": "", "DB_USER": "",
#    "DB_PASSWORD": "", "DB_SSLMODE": "", "DATABASE_URL": "",
#    "DB_POOL_SIZE": 5, "DB_MAX_OVERFLOW": 10, "DB_POOL_RECYCLE": 1800,
#    "DB_ECHO": false}
# config.get("DB_HOST") pulls the "DB_HOST" field out; an env var still overrides.
COORDINATOR_DB_CONNECTION = "COORDINATOR_DB_CONNECTION"
COORDINATOR_DB_CONNECTION_FIELDS = (
    "DATABASE_URL",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_SSLMODE",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_RECYCLE",
    "DB_POOL_TIMEOUT",
    "DB_ECHO",
)

#: keys that come from a shared JSON secret instead of a secret of their own.
#: { logical_key : secret_name_holding_a_json_object }
BUNDLED_SECRETS: dict[str, str] = {
    **{k: COORDINATOR_DB_CONNECTION for k in COORDINATOR_DB_CONNECTION_FIELDS},
    **{k: AGENTMAIL_CONFIG for k in AGENTMAIL_CONFIG_FIELDS},
    **{k: MS365_MAIL for k in MS365_MAIL_FIELDS},
}

# --- LLM agents (agentkit.py) ----------------------------------------
# No GLOBAL_/DEFAULT_ chain. Every agent uses ONE model-profile JSON secret,
# named by LLM_PROFILE (default "GEMINI_LITE"), e.g.:
#   GEMINI_LITE = {
#     "MODEL":          "gemini/gemini-3.1-flash-lite",
#     "TEMPERATURE":    0,
#     "MAX_TOKENS":     8192,
#     "GEMINI_API_KEY": "..."       # or "API_KEY" / "API_BASE"
#   }
# Read whole with config.get_json(config.get("LLM_PROFILE", "GEMINI_LITE")).
# Per-agent overrides still win: <AGENT>_MODEL / _PROVIDER / _TEMPERATURE /
# _MAX_TOKENS / _API_KEY / _API_BASE.
LLM_PROFILE = "LLM_PROFILE"
LLM_PROFILE_DEFAULT = "GEMINI_LITE"
LLM_PROFILE_FIELDS = ("MODEL", "TEMPERATURE", "MAX_TOKENS",
                      "API_KEY", "API_BASE", "GEMINI_API_KEY")

#: Reference set of the statically-known keys (dynamic agent keys not included).
ALL_KEYS = frozenset(
    [v for k, v in dict(globals()).items()
     if k.isupper() and isinstance(v, str) and k == v]
    + list(BUNDLED_SECRETS)          # fields that live inside a JSON secret
)


# ===========================================================================
#  Resolver
# ===========================================================================
def _stringify(value: Any) -> Optional[str]:
    """Normalise a JSON scalar to the string form get()/get_int()/get_bool() expect."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return str(value)


class Config:
    """env var  ->  Secret Manager secret of the same name.  Fetched fresh on every call - no caching."""

    def __init__(self, project_id: Optional[str] = None) -> None:
        self._project_id = project_id or os.environ.get(GCP_PROJECT_ID, "")
        self._sm: Optional[SecretManager] = None

    # -- lazily build the Secret Manager client (needs GCP creds) --------- #
    # This only holds the API client/transport, never a secret value, so it's
    # kept across calls - rebuilding it per lookup would just add latency.
    @property
    def secret_manager(self) -> "SecretManager":
        if self._sm is None:
            if not self._project_id:
                raise RuntimeError(
                    "GCP_PROJECT_ID is not set - cannot reach Secret Manager"
                )
            from secret_manager import SecretManager  # used as-is

            self._sm = SecretManager(self._project_id)
        return self._sm

    @property
    def project_id(self) -> str:
        return self._project_id

    # -- core ----------------------------------------------------------- #
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        val = self._resolve(key)
        return default if val is None or val == "" else val

    def _resolve(self, key: str) -> Optional[str]:
        # 1. real environment variable (overrides even a bundled field)
        env_val = os.environ.get(key)
        if env_val is not None and env_val != "":
            return env_val
        # 2. field inside a shared JSON secret (e.g. COORDINATOR_DB_CONNECTION)
        bundle_secret = BUNDLED_SECRETS.get(key)
        if bundle_secret:
            blob = self._bundle(bundle_secret)
            return _stringify(blob.get(key)) if blob else None
        # 3. Secret Manager secret whose id == the key
        try:
            secret_val = self.secret_manager.get_secrets(key)
        except Exception:  # noqa: BLE001 - not found / no creds / permission
            return None
        return secret_val or None

    def _bundle(self, secret_name: str) -> Optional[dict]:
        """Fetch + parse a JSON-object secret fresh every call; env var of the same name wins."""
        raw = os.environ.get(secret_name)
        if not raw:
            try:
                raw = self.secret_manager.get_secrets(secret_name)
            except Exception:  # noqa: BLE001
                raw = None
        try:
            parsed = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            parsed = None
        return parsed if isinstance(parsed, dict) else None

    def require(self, key: str) -> str:
        val = self.get(key)
        if val is None or val == "":
            src = BUNDLED_SECRETS.get(key)
            where = (
                f"field '{key}' of JSON secret '{src}'" if src
                else f"env var or Secret Manager secret '{key}'"
            )
            raise RuntimeError(
                f"Missing required config '{key}': not found ({where}) in project "
                f"'{self._project_id or '<unset>'}'"
            )
        return val

    # -- typed helpers ------------------------------------------------ #
    def get_int(self, key: str, default: Optional[int] = None) -> Optional[int]:
        raw = self.get(key)
        return int(raw) if raw is not None else default

    def get_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        raw = self.get(key)
        return float(raw) if raw is not None else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        raw = self.get(key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    # -- whole JSON-object secrets (e.g. GOOGLE_MAIL) ----------------- #
    def get_json(self, key: str, default: Optional[dict] = None) -> Optional[dict]:
        """Parse a JSON-object secret to a dict; an env var of the same name wins."""
        blob = self._bundle(key)
        return blob if blob is not None else default

    def require_json(self, key: str) -> dict:
        blob = self._bundle(key)
        if not blob:
            raise RuntimeError(
                f"Missing required JSON secret '{key}': not a valid JSON object in "
                f"env or Secret Manager (project '{self._project_id or '<unset>'}')"
            )
        return blob

    def __getitem__(self, key: str) -> str:
        return self.require(key)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None


#: import this everywhere
config = Config()
