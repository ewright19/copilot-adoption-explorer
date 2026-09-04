"""One-time bootstrap: create the app registration + app-only Graph consent, emit config/app.json.

Uses raw device-code flow (15-minute window) rather than the Graph PowerShell SDK,
whose device flow cancels after 120 seconds.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import uuid

import requests

def _tenant_from_argv() -> str:
    """Tenant must be explicit - never default to whatever tenant this was built against."""
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    print(
        "Usage: python src/bootstrap.py <tenant-id-or-domain>\n\n"
        "  e.g. python src/bootstrap.py contoso.onmicrosoft.com\n"
        "       python src/bootstrap.py 00000000-1111-2222-3333-444444444444\n\n"
        "Find it in Entra admin center > Overview > Tenant ID.",
        file=sys.stderr,
    )
    raise SystemExit(2)


TENANT = _tenant_from_argv()
# "Microsoft Graph Command Line Tools" - well-known first-party public client
PUBLIC_CLIENT = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
APP_NAME = "Copilot Adoption Explorer"

REQUIRED = [
    "User.Read.All",
    "Organization.Read.All",
    "Reports.Read.All",
    "ReportSettings.ReadWrite.All",
    "AiEnterpriseInteraction.Read.All",
]

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
CFG.mkdir(exist_ok=True)


def device_login() -> str:
    scopes = "Application.ReadWrite.All AppRoleAssignment.ReadWrite.All Directory.Read.All offline_access"
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

    def patch(self, path: str, body: dict) -> None:
        r = requests.patch(f"https://graph.microsoft.com/v1.0{path}", headers=self.h, json=body, timeout=120)
        if r.status_code not in (200, 204):
            raise SystemExit(f"PATCH {path} -> {r.status_code}: {r.text[:800]}")


def main() -> None:
    g = G(device_login())

    who = g.get("/me?$select=userPrincipalName")
    print(f"Signed in as {who.get('userPrincipalName')}", flush=True)

    graph_sp = g.get(f"/servicePrincipals?$filter=appId eq '{GRAPH_APP_ID}'")["value"][0]
    by_value = {r["value"]: r for r in graph_sp["appRoles"]
                if "Application" in r.get("allowedMemberTypes", [])}

    roles = []
    for name in REQUIRED:
        role = by_value.get(name)
        if not role:
            print(f"  !! permission not available in tenant: {name}", flush=True)
            continue
        roles.append({"name": name, "id": role["id"]})
    print(f"Resolved {len(roles)}/{len(REQUIRED)} app-only permissions", flush=True)

    # Always create a uniquely named app; never grant privileges to an app
    # selected solely by a display-name match.
    app_name = f"{APP_NAME} - {uuid.uuid4().hex[:8]}"
    app = g.post("/applications", {
        "displayName": app_name,
        "signInAudience": "AzureADMyOrg",
        "description": "Read-only collector for user-level M365 Copilot adoption + prompt trends.",
    })
    print(f"Created app {app['appId']}", flush=True)

    g.patch(f"/applications/{app['id']}", {
        "requiredResourceAccess": [{
            "resourceAppId": GRAPH_APP_ID,
            "resourceAccess": [{"id": r["id"], "type": "Role"} for r in roles],
        }]
    })

    sps = g.get(f"/servicePrincipals?$filter=appId eq '{app['appId']}'")["value"]
    sp = sps[0] if sps else g.post("/servicePrincipals", {"appId": app["appId"]})

    already = {a["appRoleId"] for a in g.get(f"/servicePrincipals/{sp['id']}/appRoleAssignments")["value"]}
    for r in roles:
        if r["id"] in already:
            print(f"  [=] {r['name']}", flush=True)
            continue
        g.post(f"/servicePrincipals/{sp['id']}/appRoleAssignedTo", {
            "principalId": sp["id"], "resourceId": graph_sp["id"], "appRoleId": r["id"],
        })
        print(f"  [+] consented {r['name']}", flush=True)

    secret = g.post(f"/applications/{app['id']}/addPassword", {
        "passwordCredential": {"displayName": f"collector-{time.strftime('%Y%m%d')}"}
    })

    cfg = {
        "tenant_id": TENANT,
        "client_id": app["appId"],
        "client_secret": secret["secretText"],
        "permissions": [r["name"] for r in roles],
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (CFG / "app.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"WROTE {CFG / 'app.json'}", flush=True)
    print("BOOTSTRAP_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
