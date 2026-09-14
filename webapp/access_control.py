"""Server-side authorization: decide which leader(s)' org a signed-in user may view.

This is the actual security boundary for the secured web app. Nothing here is
sent to the browser - it only produces an authorized Scope, which app.py then
uses to build a FILTERED payload (via build_report.filter_payload_for_leaders)
before rendering anything. A tenant-admin scope is the one case where the
payload is passed through unfiltered, by design.
"""
from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import build_report  # noqa: E402
from graph_client import GraphClient  # noqa: E402

ACCESS_CFG = ROOT / "config" / "access_control.json"

# Entra ID built-in directory role template ids that imply the signed-in user is
# already entitled to tenant-wide Copilot usage reporting in the admin center.
# Keys are roleTemplateId GUIDs, values are the friendly name used in the UI.
TENANT_ADMIN_ROLE_TEMPLATES: dict[str, str] = {
    "62e90394-69f5-4237-9190-012177145e10": "Global Administrator",
    "f2ef992c-3afb-46b9-b7cf-a126ee74c451": "Global Reader",
    "4a5d8f65-41da-4de4-8968-e035b65339cf": "Reports Reader",
    "75934031-6c7e-415a-99d7-48dbd49e875e": "Usage Summary Reports Reader",
    "29232cdf-9323-42fd-ade2-1d097af3e4de": "Exchange Administrator",
    "729827e3-9c14-49f7-bb1b-9608f156bbb8": "Helpdesk Administrator",
}

# Roles that are safe to auto-grant by default. Exchange/Helpdesk admins are
# recognized above so they can be opted in explicitly, but are NOT granted a
# tenant-wide usage view unless listed in extraAdminRoleTemplateIds.
DEFAULT_ADMIN_ROLE_TEMPLATES: frozenset[str] = frozenset({
    "62e90394-69f5-4237-9190-012177145e10",  # Global Administrator
    "f2ef992c-3afb-46b9-b7cf-a126ee74c451",  # Global Reader
    "4a5d8f65-41da-4de4-8968-e035b65339cf",  # Reports Reader
    "75934031-6c7e-415a-99d7-48dbd49e875e",  # Usage Summary Reports Reader
})


class AccessDenied(Exception):
    """Raised when a signed-in user has no authorized scope at all."""


@dataclass
class Scope:
    """The authorized view for one signed-in user.

    full_tenant=True means "every user in the snapshot" and takes precedence
    over leader_indices. Otherwise only the direct reports of leader_indices
    are ever serialized.
    """

    leader_indices: set[int] = field(default_factory=set)
    full_tenant: bool = False
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.full_tenant or bool(self.leader_indices)


def load_access_control() -> dict:
    if not ACCESS_CFG.exists():
        return {"adminGroupId": "", "leaders": []}
    return json.loads(ACCESS_CFG.read_text(encoding="utf-8"))


def _admin_role_templates(acl: dict) -> set[str]:
    """Which directory role template ids grant a tenant-wide view."""
    if acl.get("tenantAdminRolesEnabled") is False:
        return set()
    allowed = set(DEFAULT_ADMIN_ROLE_TEMPLATES)
    for rid in acl.get("extraAdminRoleTemplateIds", []) or []:
        rid = (rid or "").strip().lower()
        if rid:
            allowed.add(rid)
    return allowed


def resolve_scope(payload: dict, app_cfg: dict, user_oid: str, user_upn: str) -> Scope:
    """Return the Scope the signed-in user (identified by their Entra ID object
    id + UPN) is authorized to view.

    Four independent ways to gain scope, any combination applies:
      1. Tenant admin - the signed-in user holds an active Entra directory role
         that already grants tenant-wide usage reporting (Global Administrator,
         Global Reader, Reports Reader, Usage Summary Reports Reader), checked
         live via Graph app-only. Grants a FULL-TENANT view: every user in the
         snapshot, not just people who report to someone.
      2. Self-match  - the signed-in UPN IS a leader in the dataset (they have
         direct reports). No configuration needed - this uses the Entra manager
         relationship already captured in the snapshot.
      3. Delegate match - config/access_control.json maps a leader's UPN to an
         Azure AD group id, and the signed-in user is a member of that group
         (checked live via Graph app-only checkMemberGroups).
      4. Admin group match - config/access_control.json sets adminGroupId, and
         the signed-in user is a member -> full-tenant view, unless
         "adminGroupFullTenant": false, in which case they get every leader's
         direct reports instead.

    Raises AccessDenied if none of the above apply.
    """
    acl = load_access_control()
    scope = Scope()

    self_idx = build_report.find_user_index_by_upn(payload, user_upn)
    if self_idx is not None and str(self_idx) in payload["mgrs"]:
        scope.leader_indices.add(self_idx)
        scope.reasons.append("your Entra direct reports")

    allowed_roles = _admin_role_templates(acl)
    if allowed_roles and user_oid:
        held = _directory_role_templates(app_cfg, user_oid)
        matched = sorted(
            TENANT_ADMIN_ROLE_TEMPLATES.get(r, r) for r in (held & allowed_roles)
        )
        if matched:
            scope.full_tenant = True
            scope.reasons.append(f"tenant-wide ({', '.join(matched)})")

    admin_group = (acl.get("adminGroupId") or "").strip()
    delegate_map: dict[str, int] = {}
    group_ids: list[str] = []
    if admin_group:
        group_ids.append(admin_group)
    for entry in acl.get("leaders", []):
        gid = (entry.get("delegateGroupId") or "").strip()
        lupn = (entry.get("leaderUpn") or "").strip()
        if not gid or not lupn:
            continue
        li = build_report.find_user_index_by_upn(payload, lupn)
        if li is not None:
            delegate_map[gid] = li
            group_ids.append(gid)

    if group_ids and user_oid:
        member_of = _check_member_groups(app_cfg, user_oid, group_ids)
        if admin_group and admin_group in member_of:
            if acl.get("adminGroupFullTenant", True):
                scope.full_tenant = True
                scope.reasons.append("tenant-wide (admin group)")
            else:
                scope.leader_indices.update(int(k) for k in payload["mgrs"].keys())
                scope.reasons.append("all leaders (admin group)")
        for gid, li in delegate_map.items():
            if gid in member_of:
                scope.leader_indices.add(li)
                leader = payload["users"][li].get("n") or payload["users"][li].get("u") or ""
                scope.reasons.append(f"delegate for {leader}" if leader else "delegate access")

    if not scope:
        raise AccessDenied(
            f"{user_upn or user_oid} is not recognized as a leader with direct "
            "reports in the latest snapshot, does not hold a tenant admin role "
            "that grants usage reporting (Global Administrator, Global Reader, "
            "Reports Reader or Usage Summary Reports Reader), and is not a "
            "member of any group configured in config/access_control.json. Ask "
            "your admin to add you to the right delegate group, or re-run the "
            "collector if your org structure recently changed."
        )
    return scope


def _directory_role_templates(app_cfg: dict, user_oid: str) -> set[str]:
    """Return the lowercased roleTemplateIds of the Entra directory roles the
    user currently holds (including roles inherited through a group).

    App-only Graph call - server-side only, never exposed to the browser.
    Requires User.Read.All or Directory.Read.All on the collector app; returns
    an empty set (fail closed) on any error so a broken or unconsented call can
    never widen access.
    """
    try:
        g = GraphClient(app_cfg["tenant_id"], app_cfg["client_id"], app_cfg["client_secret"])
    except Exception:
        return set()

    urls = [
        f"/v1.0/users/{user_oid}/transitiveMemberOf/microsoft.graph.directoryRole"
        "?$select=id,displayName,roleTemplateId&$top=200",
        # Fallback for tenants/apps where the transitive cast is rejected.
        f"/v1.0/users/{user_oid}/memberOf/microsoft.graph.directoryRole"
        "?$select=id,displayName,roleTemplateId&$top=200",
    ]
    for url in urls:
        try:
            found = {
                (item.get("roleTemplateId") or "").strip().lower()
                for item in g.paged(url, tolerate=(400, 403, 404))
            }
        except Exception:
            continue
        found.discard("")
        if found:
            return found
    return set()


def _check_member_groups(app_cfg: dict, user_oid: str, group_ids: list[str]) -> set[str]:
    """App-only Graph call - server-side only, never exposed to the browser.
    Requires GroupMember.Read.All (or Directory.Read.All) on the collector app.
    """
    try:
        g = GraphClient(app_cfg["tenant_id"], app_cfg["client_id"], app_cfg["client_secret"])
        resp = g.post_json(f"/v1.0/users/{user_oid}/checkMemberGroups", {"groupIds": group_ids})
        if "_error" in resp:
            return set()
        return set(resp.get("value", []))
    except Exception:
        # Fail closed: a broken group check must never widen access.
        return set()


def _check_member_groups(app_cfg: dict, user_oid: str, group_ids: list[str]) -> set[str]:
    """App-only Graph call - server-side only, never exposed to the browser.
    Requires GroupMember.Read.All (or Directory.Read.All) on the collector app.
    """
    try:
        g = GraphClient(app_cfg["tenant_id"], app_cfg["client_id"], app_cfg["client_secret"])
        resp = g.post_json(f"/v1.0/users/{user_oid}/checkMemberGroups", {"groupIds": group_ids})
        if "_error" in resp:
            return set()
        return set(resp.get("value", []))
    except Exception:
        # Fail closed: a broken group check must never widen access.
        return set()
