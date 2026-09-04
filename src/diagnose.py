"""Smoke-test the tenant: report settings, users, Copilot SKUs, usage report, interaction history."""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from graph_client import GraphClient, GraphError  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))
g = GraphClient(cfg["tenant_id"], cfg["client_id"], cfg["client_secret"])


def hdr(t):
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}", flush=True)


hdr("1. Report anonymization setting")
try:
    s = g.get_json("/beta/admin/reportSettings")
    print("   current:", s.get("displayConcealedNames"))
    if s.get("displayConcealedNames"):
        r = g._request("PATCH", "/beta/admin/reportSettings",
                       json_body={"displayConcealedNames": False})
        print("   -> disabled concealment, status", r.status_code)
except GraphError as e:
    print("   ERR", e)

hdr("2. Organization")
try:
    org = g.get_json("/v1.0/organization")["value"][0]
    print("  ", org["displayName"], "|", org["id"])
except Exception as e:
    print("   ERR", e)

hdr("3. Subscribed SKUs (looking for Copilot)")
skus = list(g.paged("/v1.0/subscribedSkus"))
for s in skus:
    pn = s.get("skuPartNumber", "")
    u = s.get("prepaidUnits", {}).get("enabled", 0)
    c = s.get("consumedUnits", 0)
    flag = "  <== COPILOT" if "COPILOT" in pn.upper() else ""
    print(f"   {pn:<45} enabled={u:<6} consumed={c}{flag}")

hdr("4. User count")
users = list(g.paged(
    "/v1.0/users?$select=id,displayName,userPrincipalName,department,jobTitle,"
    "accountEnabled,assignedLicenses,userType&$top=999"))
print(f"   total users: {len(users)}")
enabled = [u for u in users if u.get("accountEnabled") and u.get("userType") != "Guest"]
print(f"   enabled members: {len(enabled)}")
copilot_sku_ids = {s["skuId"] for s in skus if "COPILOT" in s.get("skuPartNumber", "").upper()}
lic = [u for u in enabled if {l["skuId"] for l in u.get("assignedLicenses", [])} & copilot_sku_ids]
print(f"   Copilot-licensed: {len(lic)}")
for u in lic[:15]:
    print(f"      - {u['displayName']:<28} {u.get('department') or '-':<20} {u['userPrincipalName']}")

hdr("5. Copilot usage user detail report (D30)")
try:
    data = g.get_json(
        "/beta/reports/getMicrosoft365CopilotUsageUserDetail(period='D30')?$format=application/json")
    vals = data.get("value", [])
    print(f"   rows: {len(vals)}")
    if vals:
        print("   available fields:")
        for k in sorted(vals[0].keys()):
            print(f"      {k}")
        print("\n   sample rows:")
        for r in vals[:5]:
            print("     ", {k: v for k, v in r.items() if "LastActivityDate" in k or k in
                            ("userPrincipalName", "displayName")})
    else:
        print("   (empty)", json.dumps(data)[:400])
except GraphError as e:
    print("   ERR", e)

hdr("6. Enterprise interaction history (real prompts) - probing licensed users")
probe = lic[:6] or enabled[:6]
for u in probe:
    url = (f"/beta/copilot/users/{u['id']}/interactionHistory/getAllEnterpriseInteractions"
           "?$top=50")
    try:
        items = list(g.paged(url, tolerate=(400, 403, 404), cap=200))
        kinds = {}
        for it in items:
            kinds[it.get("interactionType", "?")] = kinds.get(it.get("interactionType", "?"), 0) + 1
        print(f"   {u['displayName']:<28} items={len(items):<5} {kinds}")
        if items:
            print("      sample:", json.dumps(
                {k: items[0].get(k) for k in
                 ("interactionType", "appClass", "createdDateTime", "conversationId")}))
    except GraphError as e:
        print(f"   {u['displayName']:<28} ERR {e.status} {e.body[:200]}")
