"""Collector: pulls directory, licences, Copilot usage report and prompt-level
interaction history into a local SQLite snapshot store.

Incremental + idempotent: re-running only adds new interactions.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from graph_client import GraphClient, GraphError  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "out" / "copilot.db"

APP_LABELS = {
    "BizChat": "Copilot Chat",
    "WebChat": "Copilot Chat (Web)",
    "PrivateChat": "Copilot Chat (Private)",
    "M365AdminCenter": "Admin Center",
    "ThirdPartyCopilot": "Third-party Copilot",
    "OfficeCopilotNotebook": "Notebook",
    "SharePoint": "SharePoint",
    "PowerPoint": "PowerPoint",
    "Whiteboard": "Whiteboard",
    "Excel": "Excel", "Word": "Word", "Outlook": "Outlook",
    "OneNote": "OneNote", "Loop": "Loop", "Teams": "Teams",
}
# Surfaces that are available without a paid Copilot licence
CHAT_SURFACES = {"Copilot Chat", "Copilot Chat (Web)", "Copilot Chat (Private)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, upn TEXT, display_name TEXT, mail TEXT,
  department TEXT, job_title TEXT, office TEXT, company TEXT,
  account_enabled INTEGER, user_type TEXT, manager_id TEXT,
  copilot_licensed INTEGER DEFAULT 0, license_skus TEXT
);
CREATE TABLE IF NOT EXISTS interactions (
  id TEXT PRIMARY KEY, user_id TEXT, created TEXT, month TEXT, day TEXT,
  interaction_type TEXT, app_class TEXT, app TEXT
);
CREATE INDEX IF NOT EXISTS ix_int_user  ON interactions(user_id);
CREATE INDEX IF NOT EXISTS ix_int_month ON interactions(month);
CREATE INDEX IF NOT EXISTS ix_int_type  ON interactions(interaction_type);
CREATE TABLE IF NOT EXISTS usage_report (
  upn TEXT, user_id TEXT, period TEXT, report_refresh TEXT,
  app TEXT, last_activity TEXT,
  PRIMARY KEY (upn, period, app)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started TEXT, finished TEXT,
  users INTEGER, interactions_new INTEGER, notes TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=60)
    c.executescript(SCHEMA)
    return c


def pretty_app(app_class: str | None) -> str:
    if not app_class:
        return "Unknown"
    leaf = app_class.split(".")[-1]
    return APP_LABELS.get(leaf, leaf)


def collect(cfg: dict, *, include_disabled: bool = False, workers: int = 8) -> dict:
    g = GraphClient(cfg["tenant_id"], cfg["client_id"], cfg["client_secret"])
    con = connect()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    notes: list[str] = []

    # ---- 0. report names must already be visible -----------------------
    try:
        s = g.get_json("/beta/admin/reportSettings")
        if s.get("displayConcealedNames"):
            raise RuntimeError(
                "report name concealment is ON. Run preflight.py and explicitly "
                "approve the tenant-wide setting change before collecting."
            )
    except GraphError as e:
        notes.append(f"reportSettings: {e.status}")

    # ---- 1. tenant SKUs -------------------------------------------------
    skus = list(g.paged("/v1.0/subscribedSkus"))
    sku_name = {s["skuId"]: s.get("skuPartNumber", "") for s in skus}
    copilot_skus = {sid for sid, n in sku_name.items() if "COPILOT" in n.upper()}
    print(f"  * {len(skus)} SKUs, {len(copilot_skus)} Copilot SKU(s)")

    # ---- 2. directory ---------------------------------------------------
    users = list(g.paged(
        "/v1.0/users?$select=id,displayName,userPrincipalName,mail,department,"
        "jobTitle,officeLocation,companyName,accountEnabled,userType,assignedLicenses"
        "&$top=999"))
    members = [u for u in users if u.get("userType") != "Guest"
               and (include_disabled or u.get("accountEnabled"))]
    print(f"  * {len(users)} users -> {len(members)} in scope")

    # ---- 3. managers via $batch ----------------------------------------
    reqs = [{"id": str(i), "url": f"/users/{u['id']}/manager?$select=id"}
            for i, u in enumerate(members)]
    res = g.batch(reqs)
    for i, u in enumerate(members):
        r = res.get(str(i), {})
        u["_manager_id"] = (r.get("body") or {}).get("id") if r.get("status") == 200 else None
    print(f"  * {sum(1 for u in members if u['_manager_id'])} manager edges")

    rows = []
    for u in members:
        assigned = [l["skuId"] for l in (u.get("assignedLicenses") or [])]
        rows.append((
            u["id"], u.get("userPrincipalName"), u.get("displayName"), u.get("mail"),
            u.get("department"), u.get("jobTitle"), u.get("officeLocation"),
            u.get("companyName"), 1 if u.get("accountEnabled") else 0,
            u.get("userType"), u["_manager_id"],
            1 if set(assigned) & copilot_skus else 0,
            json.dumps([sku_name.get(s, s) for s in assigned]),
        ))
    con.execute("DELETE FROM users")
    con.executemany("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()

    # ---- 4. Copilot usage report (corroborating signal) -----------------
    for period in ("D30", "D90", "D180"):
        try:
            data = g.get_json(
                f"/beta/reports/getMicrosoft365CopilotUsageUserDetail(period='{period}')"
                "?$format=application/json")
            urows = []
            for r in data.get("value", []):
                refresh = r.get("reportRefreshDate")
                for k, v in r.items():
                    if k.endswith("LastActivityDate") and v:
                        app = k[:-len("LastActivityDate")]
                        app = APP_LABELS.get(app[0].upper() + app[1:], app)
                        urows.append((r.get("userPrincipalName"), None, period, refresh, app, v))
            if urows:
                con.executemany(
                    "INSERT OR REPLACE INTO usage_report VALUES (?,?,?,?,?,?)", urows)
            print(f"  * usage report {period}: {len(data.get('value', []))} rows, "
                  f"{len(urows)} activity dates")
        except GraphError as e:
            notes.append(f"usage report {period}: {e.status}")
    con.commit()

    # ---- 5. interaction history (the real prompt data) ------------------
    lock = threading.Lock()
    total_new = 0
    done = 0

    def pull(u: dict) -> list[tuple]:
        url = (f"/beta/copilot/users/{u['id']}/interactionHistory/"
               "getAllEnterpriseInteractions?$top=200")
        out = []
        try:
            for it in g.paged(url, tolerate=(400, 403, 404)):
                created = it.get("createdDateTime") or ""
                iid = it.get("id") or hashlib.sha1(
                    f"{u['id']}|{created}|{it.get('interactionType')}|"
                    f"{it.get('appClass')}|{it.get('conversationId')}".encode()
                ).hexdigest()
                out.append((iid, u["id"], created, created[:7], created[:10],
                            it.get("interactionType"), it.get("appClass"),
                            pretty_app(it.get("appClass"))))
        except GraphError as e:
            with lock:
                notes.append(f"interactions {u.get('userPrincipalName')}: {e.status}")
        return out

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(pull, u): u for u in members}
        for fut in cf.as_completed(futs):
            batch = fut.result()
            with lock:
                if batch:
                    cur = con.executemany(
                        "INSERT OR IGNORE INTO interactions VALUES (?,?,?,?,?,?,?,?)", batch)
                    total_new += cur.rowcount
                done += 1
                if done % 10 == 0 or done == len(members):
                    print(f"    interactions {done}/{len(members)}", flush=True)
    con.commit()

    finished = dt.datetime.now(dt.timezone.utc).isoformat()
    con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                (run_id, started, finished, len(members), total_new, "; ".join(notes[:40])))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)", (finished,))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('tenant_id', ?)", (cfg["tenant_id"],))
    con.commit()

    stats = {
        "users": len(members),
        "licensed": con.execute("SELECT COUNT(*) FROM users WHERE copilot_licensed=1").fetchone()[0],
        "interactions_total": con.execute("SELECT COUNT(*) FROM interactions").fetchone()[0],
        "prompts_total": con.execute(
            "SELECT COUNT(*) FROM interactions WHERE interaction_type='userPrompt'").fetchone()[0],
        "interactions_new": total_new,
        "months": con.execute(
            "SELECT COUNT(DISTINCT month) FROM interactions WHERE month<>''").fetchone()[0],
        "notes": notes,
    }
    con.close()
    return stats


if __name__ == "__main__":
    cfg = json.loads((ROOT / "config" / "app.json").read_text(encoding="utf-8"))
    t0 = time.time()
    print("Collecting…")
    st = collect(cfg)
    print(json.dumps(st, indent=2))
    print(f"done in {time.time() - t0:.1f}s -> {DB}")
