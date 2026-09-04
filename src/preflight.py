"""Preflight check - run this FIRST in a new tenant.

    python src\\preflight.py

Verifies Python version, dependencies, config, credentials, each required Graph
permission, and whether the tenant actually returns usable Copilot data.
Exits non-zero if anything would block a collection run.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

OK, WARN, FAIL = "  [ok]  ", "  [warn]", "  [FAIL]"
problems: list[str] = []
warnings: list[str] = []


def fail(msg: str, fix: str) -> None:
    print(f"{FAIL} {msg}\n         fix: {fix}")
    problems.append(msg)


def warn(msg: str, note: str = "") -> None:
    print(f"{WARN} {msg}" + (f"\n         {note}" if note else ""))
    warnings.append(msg)


def ok(msg: str) -> None:
    print(f"{OK} {msg}")


def main() -> int:
    print("\nCopilot Adoption Explorer - preflight\n" + "=" * 60)

    # 1. Python
    print("\n1. Runtime")
    v = sys.version_info
    if v < (3, 9):
        fail(f"Python {v.major}.{v.minor} is too old", "install Python 3.9 or newer")
    else:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")

    # 2. Dependencies
    print("\n2. Dependencies")
    for mod, why in (("requests", "Graph calls"), ("openpyxl", "Excel export")):
        try:
            __import__(mod)
            ok(f"{mod} ({why})")
        except ImportError:
            fail(f"{mod} is missing ({why})", "pip install -r requirements.txt")

    # 3. Config
    print("\n3. Configuration")
    cfg_path = ROOT / "config" / "app.json"
    if not cfg_path.exists():
        fail("config/app.json not found",
             "python src\\bootstrap.py <tenant-id-or-domain>")
        return report()
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"config/app.json is not valid JSON ({e})", "re-run bootstrap.py")
        return report()

    missing = [k for k in ("tenant_id", "client_id", "client_secret") if not cfg.get(k)]
    if missing:
        fail(f"config/app.json missing: {', '.join(missing)}", "re-run bootstrap.py")
        return report()
    ok(f"tenant {cfg['tenant_id']}")
    ok(f"client {cfg['client_id']}")

    # 4. Token
    print("\n4. Authentication")
    from graph_client import GraphClient, GraphError
    gc = GraphClient(cfg["tenant_id"], cfg["client_id"], cfg["client_secret"])
    try:
        gc._access_token()
        ok("acquired an app-only access token")
    except Exception as e:
        fail(f"could not acquire a token: {e}",
             "the client secret may have expired - re-run bootstrap.py")
        return report()

    # 5. Permissions - exercise the real endpoints
    print("\n5. Graph permissions (live calls)")
    checks = [
        ("User.Read.All", "/v1.0/users?$top=1"),
        ("Organization.Read.All", "/v1.0/subscribedSkus"),
        ("ReportSettings.ReadWrite.All", "/beta/admin/reportSettings"),
        ("Reports.Read.All",
         "/beta/reports/getMicrosoft365CopilotUsageUserDetail(period='D30')"),
    ]
    for perm, path in checks:
        try:
            r = gc.get_json(path, tolerate=(401, 403))
            if r.get("_error") in (401, 403):
                fail(f"{perm} denied",
                     "grant the APPLICATION permission, then 'Grant admin consent' in Entra")
            else:
                ok(perm)
        except GraphError as e:
            warn(f"{perm} returned HTTP {e.status}", "may be transient; re-run preflight")
        except Exception as e:
            warn(f"{perm} check inconclusive: {e}")

    # 6. Tenant data readiness
    print("\n6. Tenant data readiness")
    try:
        s = gc.get_json("/beta/admin/reportSettings", tolerate=(401, 403))
        if s.get("_error"):
            warn("could not read report settings")
        elif s.get("displayConcealedNames"):
            warn("report name concealment is ON - user names are hashed",
                 "the collector turns this OFF (a TENANT-WIDE change). Get customer sign-off.")
            # Require explicit confirmation before proceeding
            response = input("\n⚠️  Confirm you have customer approval to disable report name concealment? (yes/no): ").strip().lower()
            if response != "yes":
                fail("concealment toggle not confirmed",
                     "Run again when you have explicit customer approval")
                return report()
            try:
                gc._request("PATCH", "/beta/admin/reportSettings",
                            json_body={"displayConcealedNames": False})
                ok("report name concealment disabled with explicit approval")
            except GraphError as e:
                fail(f"could not disable report name concealment (HTTP {e.status})",
                     "verify ReportSettings.ReadWrite.All and retry")
                return report()
        else:
            ok("report name concealment is off")
    except Exception:
        warn("could not read report settings")

    cop: list = []
    try:
        skus = gc.get_json("/v1.0/subscribedSkus").get("value", [])
        cop = [s for s in skus if "COPILOT" in (s.get("skuPartNumber") or "").upper()]
        if not cop:
            fail("no Copilot SKUs found in this tenant",
                 "confirm Copilot licences are purchased and assigned")
        else:
            for s in cop:
                u = s.get("prepaidUnits", {}).get("enabled", 0)
                ok(f"{s['skuPartNumber']}: {s.get('consumedUnits', 0)}/{u} seats assigned")
    except Exception as e:
        warn(f"could not read SKUs: {e}")

    # Prompt history is the engine - prove it returns data for real licensed users.
    # NOTE: a per-user 403 is normal for rooms/bots/unlicensed accounts, so only
    # sample people who actually hold a Copilot licence before judging permissions.
    try:
        cop_ids = {s["skuId"] for s in cop} if cop else set()
        licensed = []
        for u in gc.paged("/v1.0/users?$top=999&$select=id,userPrincipalName,assignedLicenses",
                          cap=999):
            if cop_ids & {lic.get("skuId") for lic in (u.get("assignedLicenses") or [])}:
                licensed.append(u)
            if len(licensed) >= 8:
                break

        if not licensed:
            warn("no Copilot-licensed users found to sample",
                 "assign at least one Copilot licence, then re-run preflight")
        else:
            found = denied = 0
            for u in licensed:
                r = gc.get_json(
                    f"/beta/copilot/users/{u['id']}/interactionHistory/"
                    "getAllEnterpriseInteractions?$top=1",
                    tolerate=(400, 401, 403, 404),
                )
                if r.get("_error") in (401, 403):
                    denied += 1
                elif r.get("value"):
                    found += 1
            if found:
                ok(f"prompt history returned for {found} of {len(licensed)} licensed users sampled")
            elif denied == len(licensed):
                fail("AiEnterpriseInteraction.Read.All denied for every licensed user sampled",
                     "consent it as an APPLICATION permission (the delegated one is self-only)")
            else:
                warn(f"no prompts yet for the {len(licensed)} licensed users sampled",
                     "likely just means they have not used Copilot yet")
    except Exception as e:
        warn(f"interaction history probe failed: {e}")

    return report()


def report() -> int:
    print("\n" + "=" * 60)
    if problems:
        print(f"NOT READY - {len(problems)} blocking issue(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    if warnings:
        print(f"READY, with {len(warnings)} warning(s) - review above.")
    else:
        print("READY - all checks passed.  Next:  python run.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
