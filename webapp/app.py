"""Secured web app: Entra ID sign-in + server-enforced, group/manager-scoped
Copilot Adoption Explorer.

Unlike the static HTML export (which embeds ALL data in the page and is only
safe to share with people who should see everything), this app authenticates
each viewer and computes their authorized scope on the SERVER before it ever
builds a response - a leader or delegate can only ever receive the JSON for
their own org, never the whole tenant.

Setup (see README.md "Secured web app deployment" section for full detail):
    1. python src/bootstrap.py <tenant>              # if not already done
    2. python src/bootstrap_webapp.py <tenant> <redirect-uri>
    3. cp config/access_control.json.example config/access_control.json
       # fill in delegateGroupId / adminGroupId as needed
    4. pip install -r requirements.txt
    5. python webapp/app.py

Run behind HTTPS in anything but local testing - session cookies are marked
Secure by default (see WEBAPP_DEV_INSECURE_COOKIES below to disable for local
http:// testing only).
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import sys
import time

import msal
from flask import Flask, redirect, request, session, url_for

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "webapp"))

import build_report  # noqa: E402
from access_control import AccessDenied, resolve_scope  # noqa: E402

CFG_DIR = ROOT / "config"


def _load(name: str, hint: str) -> dict:
    p = CFG_DIR / name
    if not p.exists():
        raise SystemExit(
            f"Missing {p}.\n{hint}"
        )
    return json.loads(p.read_text(encoding="utf-8"))


APP_CFG = _load(
    "app.json",
    "Run: python src/bootstrap.py <tenant-id-or-domain>",
)
WEB_CFG = _load(
    "webapp.json",
    "Run: python src/bootstrap_webapp.py <tenant-id-or-domain> <redirect-uri>",
)

AUTHORITY = f"https://login.microsoftonline.com/{WEB_CFG['tenant_id']}"
SCOPES = ["User.Read"]

# Set to "1" only for local http:// testing. NEVER in production - it disables
# the Secure flag on the session cookie, which is required once real Copilot
# usage data is at stake.
DEV_INSECURE_COOKIES = os.environ.get("WEBAPP_DEV_INSECURE_COOKIES") == "1"

app = Flask(__name__)
app.secret_key = WEB_CFG.get("session_secret") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not DEV_INSECURE_COOKIES,
    PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
)

# Small in-memory cache so every page view doesn't re-read + re-aggregate the
# whole SQLite snapshot; refreshed automatically after a new collection run.
_payload_cache: dict = {"payload": None, "built_at": 0.0}
_CACHE_TTL_SECONDS = 300


def _get_payload() -> dict:
    now = time.time()
    if _payload_cache["payload"] is None or (now - _payload_cache["built_at"]) > _CACHE_TTL_SECONDS:
        _payload_cache["payload"] = build_report.build_payload()
        _payload_cache["built_at"] = now
    return _payload_cache["payload"]


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        WEB_CFG["client_id"], authority=AUTHORITY,
        client_credential=WEB_CFG["client_secret"],
    )


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.route("/login")
def login():
    flow = _msal_app().initiate_auth_code_flow(SCOPES, redirect_uri=WEB_CFG["redirect_uri"])
    session["flow"] = flow
    return redirect(flow["auth_uri"])


@app.route("/auth/callback")
def auth_callback():
    flow = session.pop("flow", None)
    if not flow:
        return redirect(url_for("login"))
    try:
        result = _msal_app().acquire_token_by_auth_code_flow(flow, request.args)
    except ValueError as e:
        return f"Sign-in failed: {e}", 401
    if "error" in result:
        return f"Sign-in failed: {result.get('error_description', result['error'])}", 401

    claims = result.get("id_token_claims", {})
    session.clear()
    session.permanent = True
    session["user"] = {
        "oid": claims.get("oid"),
        "upn": claims.get("preferred_username") or claims.get("upn") or "",
        "name": claims.get("name") or "",
    }
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    post_logout = WEB_CFG.get("post_logout_redirect_uri") or request.url_root
    return redirect(f"{AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={post_logout}")


@app.route("/")
def dashboard():
    user = session.get("user")
    if not user:
        return redirect(url_for("login"))

    payload = _get_payload()
    try:
        scope = resolve_scope(payload, APP_CFG, user["oid"], user["upn"])
    except AccessDenied as e:
        return (
            "<div style='font-family:sans-serif;max-width:640px;margin:80px auto'>"
            "<h2>No Copilot data is available for your account</h2>"
            f"<p>{e}</p><p><a href='/logout'>Sign out</a></p></div>",
            403,
        )

    filtered = (
        payload if scope.full_tenant
        else build_report.filter_payload_for_leaders(payload, scope.leader_indices)
    )
    what = " &middot; ".join(scope.reasons) if scope.reasons else "your authorized scope"
    banner = (
        "<div class='card' style='display:flex;justify-content:space-between;"
        "align-items:center;margin-bottom:12px;gap:12px'>"
        f"<div>Signed in as <b>{_esc(user['name'])}</b> "
        f"({_esc(user['upn'])}) &middot; showing {_esc_keep(what)}</div>"
        "<div><a href='/logout'>Sign out</a></div></div>"
    )
    return build_report.render_html(filtered, user_banner=banner)


def _esc_keep(s: str) -> str:
    """Escape user-derived text but keep the &middot; separators we inserted."""
    return _esc(s).replace("&amp;middot;", "&middot;")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    if not WEB_CFG.get("redirect_uri", "").startswith("https://") and not DEV_INSECURE_COOKIES:
        print(
            "WARNING: redirect_uri is not https:// and WEBAPP_DEV_INSECURE_COOKIES is not "
            "set - session cookies will be rejected by browsers unless you're testing over "
            "https. For local testing only, run with:\n"
            "  set WEBAPP_DEV_INSECURE_COOKIES=1   (Windows cmd)\n"
            "  $env:WEBAPP_DEV_INSECURE_COOKIES=1  (PowerShell)\n"
        )
    port = int(WEB_CFG.get("port", 5000))
    app.run(host="127.0.0.1", port=port, debug=False)
