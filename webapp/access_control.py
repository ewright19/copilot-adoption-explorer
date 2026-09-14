"""Server-side authorization: decide which leader(s)' org a signed-in user may view.

This is the actual security boundary for the secured web app. Nothing here is
sent to the browser - it only produces a set of leader indices, which app.py
then uses to build a FILTERED payload (via build_report.filter_payload_for_leaders)
before rendering anything.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import build_report  # noqa: E402
from graph_client import GraphClient  # noqa: E402

ACCESS_CFG = ROOT / "config" / "access_control.json"


class AccessDenied(Exception):
    """Raised when a signed-in user has no authorized scope at all."""


def load_access_control() -> dict:
    if not ACCESS_CFG.exists():
        return {"adminGroupId": "", "leaders": []}
    return json.loads(ACCESS_CFG.read_text(encoding="utf-8"))


def resolve_scope(payload: dict, app_cfg: dict, user_oid: str, user_upn: str) -> set[int]:
    """Return the set of leader indices (keys of payload['mgrs'], as ints) that
    the signed-in user (identified by their Entra ID object id + UPN) is
    authorized to view.

    Three independent ways to gain scope, any combination applies:
      1. Self-match  - the signed-in UPN IS a leader in the dataset (they have
         direct reports). No configuration needed - this just works from the
         directory data already collected.
      2. Delegate match - config/access_control.json maps a leader's UPN to an
         Azure AD group id, and the signed-in user is a member of that group
         (checked live via Graph app-only checkMemberGroups).
      3. Admin match - config/access_control.json sets adminGroupId, and the
         signed-in user is a member -> full-tenant view (every leader's org).

    Raises AccessDenied if none of the above apply.
    """
    acl = load_access_control()
    scope: set[int] = set()

    self_idx = build_report.find_user_index_by_upn(payload, user_upn)
    if self_idx is not None and str(self_idx) in payload["mgrs"]:
        scope.add(self_idx)

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
            scope.update(int(k) for k in payload["mgrs"].keys())
        for gid, li in delegate_map.items():
            if gid in member_of:
                scope.add(li)

    if not scope:
        raise AccessDenied(
            f"{user_upn or user_oid} is not recognized as a leader with direct "
            "reports in the latest snapshot, and is not a member of any group "
            "configured in config/access_control.json. Ask your admin to add "
            "you to the right delegate group, or re-run the collector if your "
            "org structure recently changed."
        )
    return scope


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
