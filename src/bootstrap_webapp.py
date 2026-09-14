"""One-time bootstrap for the SECURED WEB APP sign-in (delegated auth code flow).

This creates a SEPARATE Entra ID app registration from the app-only collector
made by bootstrap.py. It authenticates the humans who open the dashboard in a
browser (leaders and their delegates) - it does NOT itself read Copilot data,
and requests only the standard "sign you in and read your profile" permission
(no admin consent required).

Usage:
    python src/bootstrap_webapp.py <tenant-id-or-domain> <redirect-uri>

    e.g. python src/bootstrap_webapp.py contoso.onmicrosoft.com http://localhost:5000/auth/callback
         python src/bootstrap_webapp.py contoso.onmicrosoft.com https://copilot-explorer.azurewebsites.net/auth/callback

Writes config/webapp.json (client_id, client_secret, tenant_id, redirect_uri,
session_secret). This file is gitignored - never commit it.
"""
from __future__ import annotations

import json
import pathlib
import secrets
import sys
import time
import uuid

import requests


def _args() -> tuple[str, str]:
    if len(sys.argv) > 2 and sys.argv[1].strip() and sys.argv[2].strip():
        return sys.argv[1].strip(), sys.argv[2].strip()
    print(
        "Usage: python src/bootstrap_webapp.py <tenant-id-or-domain> <redirect-uri>\n\n"
        "  e.g. python src/bootstrap_webapp.py contoso.onmicrosoft.com "
        "http://localhost:5000/auth/callback\n\n"
        "The redirect URI must exactly match where the web app will be reachable "
        "(add https://<host>/auth/callback once you know your production hostname; "
        "you can re-run this script later to update it, or add extra redirect URIs "
        "in Entra admin center > App registrations > your app > Authentication).",
        file=sys.stderr,
    )
    raise SystemExit(2)


TENANT, REDIRECT_URI = _args()
# "Microsoft Graph Command Line Tools" - well-known first-party public client
PUBLIC_CLIENT = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
# Microsoft Graph delegated scope id for "User.Read" (Sign in and read user profile)
USER_READ_DELEGATED_ID = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"
APP_NAME = "Copilot Adoption Explorer - Web Sign-in"

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
CFG.mkdir(exist_ok=True)


def device_login() -> str:
    scopes = "Application.ReadWrite.All offline_access"
    r = requests.post(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/devicecode",
        data={"client_id": PUBLIC_CLIENT, "scope": scopes}, timeout=60,
    )
    r.raise_for_status()
    flow = r.json()
    print("DEVICE_CODE=" + flow["user_code"], flush=True)
    print("VERIFY_URL=" + flow["verification_uri"], flush=True)
    print(flow["message"], flush=True)

    deadline = time.time() + int(flow.get("expires_in", 900))
    interval = int(flow.get("interval", 5))
    while time.time() < deadline:
        time.sleep(interval)
        t = requests.post(
            f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": PUBLIC_CLIENT,
                "device_code": flow["device_code"],
            }, timeout=60,
        )
        body = t.json()
        if t.status_code == 200:
            print("AUTH_OK", flush=True)
            return body["access_token"]
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise SystemExit(f"Device auth failed: {body}")
    raise SystemExit("Device auth timed out after 15 minutes.")


class G:
    def __init__(self, token: str):
        self.h = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    def get(self, path: str) -> dict:
        r = requests.get(f"https://graph.microsoft.com/v1.0{path}", headers=self.h, timeout=120)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict, ok=(200, 201, 204)) -> dict:
        r = requests.post(f"https://graph.microsoft.com/v1.0{path}", headers=self.h, json=body, timeout=120)
        if r.status_code not in ok:
            raise SystemExit(f"POST {path} -> {r.status_code}: {r.text[:800]}")
        return r.json() if r.content else {}


def main() -> None:
    g = G(device_login())

    who = g.get("/me?$select=userPrincipalName")
    print(f"Signed in as {who.get('userPrincipalName')}", flush=True)

    # Always create a uniquely named app; never grant privileges to an app
    # selected solely by a display-name match (see bootstrap.py for why).
    app_name = f"{APP_NAME} - {uuid.uuid4().hex[:8]}"
    app = g.post("/applications", {
        "displayName": app_name,
        "signInAudience": "AzureADMyOrg",
        "web": {
            "redirectUris": [REDIRECT_URI],
            "implicitGrantSettings": {
                "enableIdTokenIssuance": False,
                "enableAccessTokenIssuance": False,
            },
        },
        "requiredResourceAccess": [{
            "resourceAppId": GRAPH_APP_ID,
            "resourceAccess": [{"id": USER_READ_DELEGATED_ID, "type": "Scope"}],
        }],
        "description": "Delegated sign-in ONLY for the secured Copilot Adoption Explorer "
                       "web app. Does not itself read Copilot data - see bootstrap.py "
                       "for the separate app-only collector registration.",
    })
    print(f"Created app {app['appId']}", flush=True)

    sps = g.get(f"/servicePrincipals?$filter=appId eq '{app['appId']}'")["value"]
    if not sps:
        g.post("/servicePrincipals", {"appId": app["appId"]})

    secret = g.post(f"/applications/{app['id']}/addPassword", {
        "passwordCredential": {"displayName": f"webapp-{time.strftime('%Y%m%d')}"}
    })

    cfg = {
        "tenant_id": TENANT,
        "client_id": app["appId"],
        "client_secret": secret["secretText"],
        "redirect_uri": REDIRECT_URI,
        "session_secret": secrets.token_hex(32),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (CFG / "webapp.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"WROTE {CFG / 'webapp.json'}", flush=True)
    print(
        "\nNOTE: each signed-in user (leader or delegate) will see a one-time "
        "Microsoft consent prompt for 'Sign you in and read your profile' - that "
        "is the standard User.Read delegated permission and needs no admin consent.\n"
        "\nNOTE: the group-membership check for delegates uses the EXISTING "
        "app-only collector credentials (config/app.json) with GroupMember.Read.All. "
        "If that app was bootstrapped before this permission was added, re-run "
        "bootstrap.py (it always creates a fresh app registration) or add "
        "GroupMember.Read.All to the existing one and grant admin consent.\n",
        flush=True,
    )
    print("BOOTSTRAP_WEBAPP_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
