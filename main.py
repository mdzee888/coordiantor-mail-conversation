"""
Coordinator service  (Cloud Run / gunicorn entrypoint: ``main:application``)
==========================================================================

One HTTP service that:

* receives inbound-mail webhooks for each active provider
      POST /agentmail/webhook
      POST /gmail/webhook            (Pub/Sub push; ?token=<GOOGLE_MAIL.push_token>)
      POST /m365/webhook            (Graph notifications + validation handshake)
  -> each runs pipeline.process()

* exposes an admin API so a **Cloud Scheduler** job can (re)create the
  provider webhooks / subscriptions *after deploy*, with no CLI access:

      POST /admin/setup            set up every active provider
      POST /admin/renew            renew every active provider   <- schedule this
      POST /admin/teardown         remove every active provider's hook
      POST /admin/status           report each provider's hook state
      POST /admin/<provider>/<action>     same, one provider only
        provider in {agentmail, gmail, m365}
        action   in {setup, renew, teardown, status}

  Auth: header ``X-Admin-Token: <ADMIN_TOKEN>`` (``Authorization: Bearer`` also
  accepted). If the ADMIN_TOKEN secret is unset the admin API is disabled (503).

* health: GET /  and  GET /healthz

Which providers are active:
  ACTIVE_PROVIDERS = "auto" (default -> detect from the secrets that resolve)
                   or "agentmail,gmail,m365"

Local:  python main.py            (werkzeug dev server)
Prod :  gunicorn main:application  (see Dockerfile)
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

from config import config

logging.basicConfig(
    level=config.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("coordinator")

PROVIDERS = ("agentmail", "gmail", "m365")
ACTIONS = ("setup", "renew", "teardown", "status")


# --------------------------------------------------------------------------- #
# provider selection
# --------------------------------------------------------------------------- #
def _detect() -> list[str]:
    active = []
    if config.get("AGENTMAIL_API_KEY"):
        active.append("agentmail")
    if config.get_json("GOOGLE_MAIL"):
        active.append("gmail")
    if config.get("MS_CLIENT_ID") and config.get("MS_CLIENT_SECRET"):
        active.append("m365")
    return active


def _wanted() -> list[str]:
    raw = (config.get("ACTIVE_PROVIDERS", "auto") or "auto").strip().lower()
    if raw in ("", "auto"):
        return _detect()
    return [p.strip() for p in raw.split(",") if p.strip() in PROVIDERS]


def _load(name: str):
    if name == "agentmail":
        from providers import agentmail as mod
    elif name == "gmail":
        from providers import gmail as mod
    elif name == "m365":
        from providers import m365 as mod
    else:
        raise ValueError(name)
    return mod


# --------------------------------------------------------------------------- #
# admin auth
# --------------------------------------------------------------------------- #
def _authorized(req) -> tuple[bool, str | None]:
    token = config.get("ADMIN_TOKEN")
    if not token:
        return False, "admin API disabled - set the ADMIN_TOKEN secret"
    # X-Admin-Token so it can coexist with a Cloud Run OIDC `Authorization` header;
    # `Authorization: Bearer <token>` is also accepted for convenience.
    supplied = req.headers.get("X-Admin-Token", "")
    if not supplied:
        auth = req.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
    if supplied and supplied == token:
        return True, None
    return False, "unauthorized"


# --------------------------------------------------------------------------- #
# app factory
# --------------------------------------------------------------------------- #
def create_app():
    active = _wanted()
    root = Flask(__name__)

    loaded: dict = {}
    mounts: dict = {}
    for name in active:
        try:
            mod = _load(name)
        except Exception as exc:  # noqa: BLE001 - partial config, bad creds, ...
            log.error("provider %s failed to load: %s", name, exc)
            continue
        loaded[name] = mod
        mounts[f"/{name}"] = mod.app

    log.info("active providers: %s", ", ".join(loaded) or "(none)")

    # ---- health -------------------------------------------------------- #
    @root.get("/")
    def index():
        return jsonify(ok=True, service="coordinator", providers=list(loaded))

    @root.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    # ---- admin -------------------------------------------------------- #
    def _run(names: list[str], action: str):
        ok, err = _authorized(request)
        if not ok:
            return jsonify(ok=False, error=err), (503 if "disabled" in err else 401)

        results, errors = {}, {}
        for name in names:
            mod = loaded.get(name)
            if mod is None:
                errors[name] = "provider not active"
                continue
            try:
                results[name] = mod.admin_op(action)
            except Exception as exc:  # noqa: BLE001
                log.exception("admin %s/%s failed", name, action)
                errors[name] = str(exc)

        status = 200 if (results and not errors) else (207 if results else 502)
        return jsonify(ok=not errors, action=action,
                       results=results, errors=errors), status

    @root.post("/admin/<action>")
    def admin_all(action):
        if action not in ACTIONS:
            return jsonify(ok=False, error=f"unknown action {action!r}"), 404
        return _run(list(loaded), action)

    @root.post("/admin/<name>/<action>")
    def admin_one(name, action):
        if name not in PROVIDERS or action not in ACTIONS:
            return jsonify(ok=False, error="unknown provider or action"), 404
        return _run([name], action)

    return DispatcherMiddleware(root, mounts) if mounts else root


#: WSGI entrypoint  ->  gunicorn main:application   (alias: main:app)
application = create_app()
app = application


if __name__ == "__main__":
    run_simple(
        config.get("HOST", "0.0.0.0"),
        config.get_int("PORT", 8080),
        application,
        use_reloader=False,
        threaded=True,
    )
