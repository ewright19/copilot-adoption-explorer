"""Build the deliverables from the SQLite snapshot:

  out/copilot-adoption-explorer.html   sandbox-safe single-file dashboard
  out/copilot-adoption.xlsx            multi-sheet workbook
  out/copilot-user-detail.csv          flat user x month export
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import json
import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "out" / "copilot.db"
OUT = ROOT / "out"

CHAT_SURFACES = {"Copilot Chat", "Copilot Chat (Web)", "Copilot Chat (Private)"}


# --------------------------------------------------------------------------
# 1. Load + shape
# --------------------------------------------------------------------------
def build_payload() -> dict:
    if not DB.exists():
        raise SystemExit(
            f"No snapshot found at {DB}\n"
            "Run a collection first:\n"
            "    python run.py\n"
            "(--build-only only works after at least one successful collection)"
        )
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "users" not in tables:
        raise SystemExit(
            f"The snapshot at {DB} is empty or incomplete.\n"
            "Re-run a full collection:\n"
            "    python run.py"
        )

    users = [dict(r) for r in con.execute(
        "SELECT id,upn,display_name,department,job_title,office,company,"
        "manager_id,copilot_licensed,license_skus FROM users ORDER BY display_name")]
    idx = {u["id"]: i for i, u in enumerate(users)}

    months = [r[0] for r in con.execute(
        "SELECT DISTINCT month FROM interactions WHERE month<>'' ORDER BY month")]

    # prompts per user per month
    per_um = collections.defaultdict(dict)
    for uid, m, c in con.execute(
            "SELECT user_id,month,COUNT(*) FROM interactions "
            "WHERE interaction_type='userPrompt' AND month<>'' GROUP BY user_id,month"):
        per_um[uid][m] = c

    # prompts per user per app
    per_ua = collections.defaultdict(dict)
    for uid, a, c in con.execute(
            "SELECT user_id,app,COUNT(*) FROM interactions "
            "WHERE interaction_type='userPrompt' GROUP BY user_id,app"):
        per_ua[uid][a] = c

    # per user per month per app (for surface trend within a scope)
    per_uma = collections.defaultdict(lambda: collections.defaultdict(dict))
    for uid, m, a, c in con.execute(
            "SELECT user_id,month,app,COUNT(*) FROM interactions "
            "WHERE interaction_type='userPrompt' AND month<>'' GROUP BY user_id,month,app"):
        per_uma[uid][m][a] = c

    # active days + first/last
    span = {}
    for uid, first, last, days in con.execute(
            "SELECT user_id,MIN(created),MAX(created),COUNT(DISTINCT day) FROM interactions "
            "WHERE interaction_type='userPrompt' GROUP BY user_id"):
        span[uid] = (first[:10] if first else "", last[:10] if last else "", days)

    # responses (for a prompt:response ratio sanity signal)
    resp = dict(con.execute(
        "SELECT user_id,COUNT(*) FROM interactions "
        "WHERE interaction_type='aiResponse' GROUP BY user_id").fetchall())

    # ---- hierarchy ----
    kids = collections.defaultdict(list)
    for u in users:
        if u["manager_id"] and u["manager_id"] in idx:
            kids[u["manager_id"]].append(u["id"])

    def descendants(root: str) -> list[str]:
        out, stack, seen = [], list(kids.get(root, [])), {root}
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            out.append(cur)
            stack.extend(kids.get(cur, []))
        return out

    mgrs = {}
    for mid in kids:
        # "o" (whole organisation) includes the leader themselves, so a leader's
        # own usage is visible in their own rollup and totals reconcile upward.
        mgrs[str(idx[mid])] = {
            "d": sorted(idx[k] for k in kids[mid]),
            "o": sorted({idx[mid]} | {idx[k] for k in descendants(mid)}),
        }

    # chain of ancestors, for the "reports up through" column
    def chain(uid: str) -> list[int]:
        out, seen = [], set()
        cur = uid
        while True:
            u = users[idx[cur]]
            m = u["manager_id"]
            if not m or m not in idx or m in seen:
                break
            seen.add(m)
            out.append(idx[m])
            cur = m
        return out

    payload_users = []
    for u in users:
        uid = u["id"]
        f, l, d = span.get(uid, ("", "", 0))
        payload_users.append({
            "n": u["display_name"] or u["upn"],
            "u": u["upn"] or "",
            "dept": u["department"] or "",
            "title": u["job_title"] or "",
            "mgr": idx.get(u["manager_id"], -1) if u["manager_id"] else -1,
            "chain": chain(uid),
            "lic": int(u["copilot_licensed"] or 0),
            "mo": per_um.get(uid, {}),
            "ap": per_ua.get(uid, {}),
            "uma": {m: a for m, a in per_uma.get(uid, {}).items()},
            "f": f, "l": l, "days": d,
            "resp": resp.get(uid, 0),
        })

    tenant = con.execute("SELECT v FROM meta WHERE k='tenant_id'").fetchone()
    last_run = con.execute("SELECT v FROM meta WHERE k='last_run'").fetchone()
    rep_rows = con.execute("SELECT COUNT(*) FROM usage_report").fetchone()[0]
    con.close()

    return {
        "meta": {
            "tenant": tenant[0] if tenant else "",
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "lastRun": (last_run[0][:16].replace("T", " ") if last_run else ""),
            "months": months,
            "usageReportRows": rep_rows,
            "chatSurfaces": sorted(CHAT_SURFACES),
        },
        "users": payload_users,
        "mgrs": mgrs,
    }


# --------------------------------------------------------------------------
# 2. Exports
# --------------------------------------------------------------------------
def write_csv(p: dict) -> pathlib.Path:
    path = OUT / "copilot-user-detail.csv"
    months = p["meta"]["months"]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Display name", "UPN", "Manager", "Reports up through",
                    "Copilot licensed", "Access type", "Total prompts", "Active days",
                    "First prompt", "Last prompt", "Top surface"] + months)
        for u in p["users"]:
            mgr = p["users"][u["mgr"]]["n"] if u["mgr"] >= 0 else ""
            up = " > ".join(p["users"][i]["n"] for i in reversed(u["chain"]))
            total = sum(u["mo"].values())
            chat_only = (not u["lic"]) and any(
                a in CHAT_SURFACES and c > 0 for a, c in u["ap"].items())
            access = "Licensed" if u["lic"] else ("Copilot Chat only" if chat_only else "No Copilot")
            top = max(u["ap"].items(), key=lambda x: x[1])[0] if u["ap"] else ""
            w.writerow([u["n"], u["u"], mgr, up, "Yes" if u["lic"] else "No", access,
                        total, u["days"], u["f"], u["l"], top]
                       + [u["mo"].get(m, 0) for m in months])
    return path


def write_xlsx(p: dict) -> pathlib.Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise SystemExit(
            "The Excel export needs openpyxl.  Install it with:\n"
            "    pip install -r requirements.txt\n"
            "(the HTML dashboard and CSV were still produced)"
        )

    months = p["meta"]["months"]
    users = p["users"]
    path = OUT / "copilot-adoption.xlsx"
    wb = Workbook()

    head_fill = PatternFill("solid", fgColor="B11F4B")
    head_font = Font(color="FFFFFF", bold=True)

    def style(ws, ncols, freeze="A2"):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.freeze_panes = freeze
        for c in range(1, ncols + 1):
            longest = max((len(str(ws.cell(row=r, column=c).value or ""))
                           for r in range(1, min(ws.max_row, 300) + 1)), default=10)
            ws.column_dimensions[get_column_letter(c)].width = min(max(longest + 2, 10), 42)

    def access_of(u):
        if u["lic"]:
            return "Licensed"
        if any(a in CHAT_SURFACES and c > 0 for a, c in u["ap"].items()):
            return "Copilot Chat only"
        return "No Copilot"

    # -- Summary --
    ws = wb.active
    ws.title = "Summary"
    lic = sum(1 for u in users if u["lic"])
    active = sum(1 for u in users if sum(u["mo"].values()) > 0)
    lic_active = sum(1 for u in users if u["lic"] and sum(u["mo"].values()) > 0)
    chat_only = sum(1 for u in users if access_of(u) == "Copilot Chat only")
    total_prompts = sum(sum(u["mo"].values()) for u in users)
    rows = [
        ("Metric", "Value"),
        ("Tenant", p["meta"]["tenant"]),
        ("Generated", p["meta"]["generated"]),
        ("Months covered", f"{months[0]} to {months[-1]}" if months else "n/a"),
        ("People in scope", len(users)),
        ("Copilot licensed", lic),
        ("Licensed and active", lic_active),
        ("Licence adoption %", round(100 * lic_active / lic, 1) if lic else 0),
        ("Copilot Chat only users (unlicensed but active)", chat_only),
        ("Any-Copilot active users", active),
        ("Total user prompts", total_prompts),
        ("Prompts per active user", round(total_prompts / active, 1) if active else 0),
        ("Unused licences", lic - lic_active),
    ]
    for r in rows:
        ws.append(list(r))
    style(ws, 2)

    # -- By leader --
    ws = wb.create_sheet("By leader")
    ws.append(["Leader", "Direct reports", "Total org (incl. leader)", "Licensed in org",
               "Active in org", "Org adoption %", "Total prompts",
               "Prompts per active user", "Heavy users", "Dormant licensed"])
    for key, m in sorted(p["mgrs"].items(), key=lambda x: -len(x[1]["o"])):
        leader = users[int(key)]
        org = [users[i] for i in m["o"]]
        olic = [u for u in org if u["lic"]]
        oact = [u for u in org if sum(u["mo"].values()) > 0]
        lic_act = [u for u in olic if sum(u["mo"].values()) > 0]
        tp = sum(sum(u["mo"].values()) for u in org)
        nmonths = max(1, len(months))
        heavy = sum(1 for u in org if sum(u["mo"].values()) / nmonths >= 40)
        ws.append([leader["n"], len(m["d"]), len(org), len(olic), len(oact),
                   round(100 * len(lic_act) / len(olic), 1) if olic else 0, tp,
                   round(tp / len(oact), 1) if oact else 0, heavy,
                   len(olic) - len(lic_act)])
    style(ws, 10)

    # -- User detail --
    ws = wb.create_sheet("User detail")
    ws.append(["Display name", "UPN", "Manager", "Reports up through", "Access type",
               "Total prompts", "Avg prompts/month", "Active days", "First prompt",
               "Last prompt", "Top surface"] + months)
    nmonths = max(1, len(months))
    for u in sorted(users, key=lambda x: -sum(x["mo"].values())):
        mgr = users[u["mgr"]]["n"] if u["mgr"] >= 0 else ""
        up = " > ".join(users[i]["n"] for i in reversed(u["chain"]))
        tot = sum(u["mo"].values())
        top = max(u["ap"].items(), key=lambda x: x[1])[0] if u["ap"] else ""
        ws.append([u["n"], u["u"], mgr, up, access_of(u), tot, round(tot / nmonths, 1),
                   u["days"], u["f"], u["l"], top]
                  + [u["mo"].get(m, 0) for m in months])
    style(ws, 11 + len(months))

    # -- Monthly trend --
    ws = wb.create_sheet("Monthly trend")
    ws.append(["Month", "Active users", "Licensed active", "Total prompts",
               "Prompts per active user"])
    for m in months:
        act = [u for u in users if u["mo"].get(m, 0) > 0]
        la = [u for u in act if u["lic"]]
        tp = sum(u["mo"].get(m, 0) for u in users)
        ws.append([m, len(act), len(la), tp, round(tp / len(act), 1) if act else 0])
    style(ws, 5)

    # -- Surfaces --
    ws = wb.create_sheet("Surfaces")
    ws.append(["Surface", "Prompts", "Distinct users", "Share %"])
    agg, usercount = collections.Counter(), collections.Counter()
    for u in users:
        for a, c in u["ap"].items():
            agg[a] += c
            usercount[a] += 1
    tot = sum(agg.values()) or 1
    for a, c in agg.most_common():
        ws.append([a, c, usercount[a], round(100 * c / tot, 1)])
    style(ws, 4)

    # -- Methodology --
    ws = wb.create_sheet("Methodology")
    for line in [
        ("Field", "Definition"),
        ("Source - prompts",
         "Microsoft Graph /beta/copilot/users/{id}/interactionHistory/getAllEnterpriseInteractions, "
         "counting interactionType = 'userPrompt'. This is the only Microsoft API that exposes "
         "per-user prompt volume."),
        ("Source - licences", "Graph /v1.0/subscribedSkus + user assignedLicenses (SKU containing 'COPILOT')."),
        ("Source - hierarchy", "Graph /v1.0/users/{id}/manager, resolved transitively to build each leader's full org."),
        ("Source - usage report",
         "Graph /beta/reports/getMicrosoft365CopilotUsageUserDetail - last-activity dates only, "
         "used as a corroborating signal. Returns blank dates when the tenant has no aggregated activity."),
        ("Access type", "Licensed = paid Copilot SKU. Copilot Chat only = no paid SKU but prompts on a chat surface."),
        ("Active user", "One or more userPrompt interactions in the selected period."),
        ("Adoption %", "Licensed users who were active / licensed users in scope."),
        ("Heavy / Moderate / Light",
         "Default thresholds on average prompts per month: Heavy >= 40, Moderate 10-39, Light 1-9, Dormant 0. "
         "Adjustable in the HTML dashboard."),
        ("Privacy note",
         "Prompt CONTENT is never collected - only counts, timestamps and the app surface. "
         "Report name concealment must be OFF in the M365 admin centre for names to resolve."),
    ]:
        ws.append(list(line))
    style(ws, 2)

    wb.save(path)
    return path


# --------------------------------------------------------------------------
# 3. Dashboard
# --------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Copilot Adoption Explorer</title>
<script>
  (() => {
    const param = new URLSearchParams(window.location.search).get("scoutTheme");
    const theme =
      param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);
  })();
</script>
<style>
:root {
  color-scheme: light;
  --cp-bg: #f7f4ef;
  --cp-bg-elevated: #fcfbf8;
  --cp-surface: #ffffff;
  --cp-surface-soft: #f5f5f5;
  --cp-border: #dedede;
  --cp-border-strong: #919191;
  --cp-text: #242424;
  --cp-text-muted: #5c5c5c;
  --cp-text-soft: #6f6f6f;
  --cp-accent: #b11f4b;
  --cp-accent-hover: #9a1a41;
  --cp-accent-soft: rgba(177, 31, 75, 0.08);
  --cp-accent-fg: #ffffff;
  --cp-success: #16a34a;
  --cp-danger: #dc2626;
  --cp-warning: #f59e0b;
  --cp-link: #0078d4;
  --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.12);
  --cp-overlay: rgba(255, 255, 255, 0.8);
  --cp-panel: rgba(255, 255, 255, 0.86);
  --cp-panel-strong: rgba(255, 255, 255, 0.96);
  --cp-sheen: rgba(255, 255, 255, 0.55);
  --cp-highlight: rgba(177, 31, 75, 0.12);
}
html[data-theme="dark"] {
  color-scheme: dark;
  --cp-bg: #3d3b3a;
  --cp-bg-elevated: #343231;
  --cp-surface: #292929;
  --cp-surface-soft: #2e2e2e;
  --cp-border: #474747;
  --cp-border-strong: #5f5f5f;
  --cp-text: #dedede;
  --cp-text-muted: #919191;
  --cp-text-soft: #b0b0b0;
  --cp-accent: #fd8ea1;
  --cp-accent-hover: #fb7b91;
  --cp-accent-soft: rgba(253, 142, 161, 0.14);
  --cp-accent-fg: #1a1a1a;
  --cp-success: #4ade80;
  --cp-danger: #f87171;
  --cp-warning: #fbbf24;
  --cp-link: #4da6ff;
  --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
  --cp-overlay: rgba(41, 41, 41, 0.88);
  --cp-panel: rgba(41, 41, 41, 0.72);
  --cp-panel-strong: rgba(41, 41, 41, 0.96);
  --cp-sheen: rgba(255, 255, 255, 0.04);
  --cp-highlight: rgba(253, 142, 161, 0.12);
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: var(--cp-bg); color: var(--cp-text);
  font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 14px; line-height: 1.5;
}
h1 { font-size: 22px; margin: 0 0 2px; }
h2 { font-size: 15px; margin: 0 0 12px; font-weight: 600; }
.sub { color: var(--cp-text-muted); font-size: 12.5px; }
.card {
  background: var(--cp-surface); border: 1px solid var(--cp-border);
  border-radius: 16px; padding: 18px;
  box-shadow: 0 0 2px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.14);
}
header.card { margin-bottom: 16px; display:flex; flex-wrap:wrap; gap:16px; align-items:flex-start; justify-content:space-between;}
.controls { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; align-items: flex-end; }
.ctl { display: flex; flex-direction: column; gap: 4px; }
.ctl label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--cp-text-soft); font-weight:600;}
select, input[type=text], input[type=number] {
  background: var(--cp-surface); color: var(--cp-text);
  border: 1px solid var(--cp-border); border-radius: 0.625rem;
  padding: 7px 10px; font-family: inherit; font-size: 13px; min-width: 150px;
}
select:focus, input:focus { outline: 2px solid var(--cp-accent); outline-offset: -1px; }
button {
  background: var(--cp-accent); color: var(--cp-accent-fg); border: none;
  border-radius: 0.625rem; padding: 8px 14px; font-family: inherit;
  font-size: 13px; font-weight: 600; cursor: pointer;
}
button:hover { background: var(--cp-accent-hover); }
button.ghost { background: transparent; color: var(--cp-text); border: 1px solid var(--cp-border); }
button.ghost:hover { background: var(--cp-surface-soft); }
button.ghost.on { background: var(--cp-accent-soft); border-color: var(--cp-accent); color: var(--cp-accent); }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); gap: 12px; margin-bottom: 16px; }
.kpi { background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 16px; padding: 14px 16px;
       box-shadow: 0 0 2px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.14); }
.kpi .v { font-size: 26px; font-weight: 700; letter-spacing: -.02em; }
.kpi .k { font-size: 11.5px; color: var(--cp-text-muted); text-transform: uppercase; letter-spacing: .04em; }
.kpi .d { font-size: 11.5px; margin-top: 3px; color: var(--cp-text-soft); }
.grid2 { display: grid; grid-template-columns: 1.55fr 1fr; gap: 16px; margin-bottom: 16px; }
@media (max-width: 950px) { .grid2 { grid-template-columns: 1fr; } }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--cp-border); }
th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--cp-text-soft);
     cursor: pointer; user-select: none; position: sticky; top: 0; background: var(--cp-surface); }
th:hover { color: var(--cp-accent); }
tbody tr:hover { background: var(--cp-accent-soft); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; border: 1px solid var(--cp-border); }
.t-heavy    { background: var(--cp-accent); color: var(--cp-accent-fg); border-color: var(--cp-accent); }
.t-moderate { background: var(--cp-accent-soft); color: var(--cp-accent); border-color: var(--cp-accent); }
.t-light    { background: var(--cp-surface-soft); color: var(--cp-text-muted); }
.t-dormant  { background: transparent; color: var(--cp-text-soft); border-style: dashed; }
.t-lic  { background: var(--cp-surface-soft); color: var(--cp-text-muted); }
.t-chat { background: transparent; color: var(--cp-link); border-color: var(--cp-link); }
.t-none { background: transparent; color: var(--cp-text-soft); border-style: dotted; }
.scroll { max-height: 560px; overflow: auto; }
.bar-row { display: grid; grid-template-columns: 132px 1fr 52px; gap: 8px; align-items: center; margin-bottom: 6px; font-size: 12.5px; }
.bar { height: 9px; border-radius: 999px; background: var(--cp-accent); }
.bar-bg { background: var(--cp-surface-soft); border-radius: 999px; overflow: hidden; }
.muted { color: var(--cp-text-muted); }
.note { font-size: 12px; color: var(--cp-text-muted); margin-top: 10px; padding-top:10px; border-top:1px solid var(--cp-border); }
textarea { width: 100%; height: 150px; margin-top: 10px; font-family: Consolas, "Courier New", Courier, monospace;
           font-size: 11.5px; background: var(--cp-surface-soft); color: var(--cp-text);
           border: 1px solid var(--cp-border); border-radius: 0.625rem; padding: 8px; }
.hide { display: none; }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:11.5px; color:var(--cp-text-muted); margin-top:8px;}
.legend i { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:5px; vertical-align:middle;}
.pathcell { font-size:11.5px; color: var(--cp-text-soft); }
</style>
</head>
<body>

<header class="card">
  <div>
    <h1>Copilot Adoption Explorer</h1>
    <div class="sub" id="subtitle"></div>
  </div>
  <div style="text-align:right">
    <button id="btnCsv">Export CSV</button>
    <button class="ghost" id="btnPrint">Print / PDF</button>
  </div>
</header>

<div class="controls card">
  <div class="ctl">
    <label for="selLeader">Leader / organisation</label>
    <select id="selLeader"></select>
  </div>
  <div class="ctl">
    <label for="selScope">Scope</label>
    <select id="selScope">
      <option value="org">Entire organisation (all levels)</option>
      <option value="direct">Direct reports only</option>
    </select>
  </div>
  <div class="ctl">
    <label for="selMonth">Period</label>
    <select id="selMonth"></select>
  </div>
  <div class="ctl">
    <label for="selAccess">Population</label>
    <select id="selAccess">
      <option value="all">Licensed + Copilot Chat</option>
      <option value="lic">Licensed only</option>
      <option value="chat">Copilot Chat only</option>
    </select>
  </div>
  <div class="ctl">
    <label for="inHeavy">Heavy &ge; (prompts/mo)</label>
    <input type="number" id="inHeavy" value="40" min="1" style="min-width:100px">
  </div>
  <div class="ctl">
    <label for="inMod">Moderate &ge;</label>
    <input type="number" id="inMod" value="10" min="1" style="min-width:100px">
  </div>
  <div class="ctl">
    <label for="inSearch">Search</label>
    <input type="text" id="inSearch" placeholder="name or email">
  </div>
</div>

<div class="kpis" id="kpis"></div>

<div class="grid2">
  <div class="card">
    <h2>Monthly prompt volume &amp; active users</h2>
    <div id="trend"></div>
    <div class="legend">
      <span><i style="background:var(--cp-accent)"></i>Prompts</span>
      <span><i style="background:var(--cp-link)"></i>Active users</span>
    </div>
  </div>
  <div class="card">
    <h2>Where Copilot is used</h2>
    <div id="surfaces"></div>
  </div>
</div>

<div class="card" style="margin-bottom:16px">
  <h2>Engagement bands <span class="muted" style="font-weight:400">— click to filter the table</span></h2>
  <div id="bands"></div>
</div>

<div class="card">
  <h2>User-level detail <span class="muted" id="rowcount" style="font-weight:400"></span></h2>
  <div class="scroll">
    <table id="tbl">
      <thead><tr>
        <th data-k="n">Person</th>
        <th data-k="mgrName">Manager</th>
        <th data-k="access">Access</th>
        <th data-k="band">Band</th>
        <th class="num" data-k="prompts">Prompts</th>
        <th class="num" data-k="permo">Per month</th>
        <th class="num" data-k="days">Active days</th>
        <th data-k="top">Top surface</th>
        <th data-k="l">Last used</th>
        <th>Trend</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div class="note" id="method"></div>
  <textarea id="csvOut" class="hide" readonly></textarea>
</div>

<script>
const DATA = __DATA_JSON__;
const U = DATA.users, MGRS = DATA.mgrs, MONTHS = DATA.meta.months;
const CHAT = new Set(DATA.meta.chatSurfaces);

const el = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

U.forEach((u, i) => {
  u.i = i;
  u.mgrName = u.mgr >= 0 ? U[u.mgr].n : "";
  u.path = u.chain.slice().reverse().map(x => U[x].n).join(" › ");
  u.chatOnly = !u.lic && Object.keys(u.ap).some(a => CHAT.has(a) && u.ap[a] > 0);
  u.access = u.lic ? "Licensed" : (u.chatOnly ? "Copilot Chat" : "No Copilot");
});

// ---------- leader list ----------
const leaders = Object.keys(MGRS).map(k => ({
  i: +k, n: U[+k].n, direct: MGRS[k].d.length, org: MGRS[k].o.length
})).sort((a, b) => b.org - a.org || a.n.localeCompare(b.n));

el("selLeader").innerHTML =
  '<option value="-1">Entire tenant (' + U.length + ' people)</option>' +
  leaders.map(l => `<option value="${l.i}">${esc(l.n)} — ${l.org} in org (incl. self) / ${l.direct} direct</option>`).join("");

el("selMonth").innerHTML =
  '<option value="ALL">All months (' + (MONTHS[0]||"") + ' – ' + (MONTHS[MONTHS.length-1]||"") + ')</option>' +
  MONTHS.slice().reverse().map(m => `<option value="${m}">${m}</option>`).join("");

el("subtitle").textContent =
  "Tenant " + DATA.meta.tenant + "  ·  generated " + DATA.meta.generated +
  "  ·  " + U.length + " people  ·  " + MONTHS.length + " months of prompt history";

// ---------- state ----------
const state = { leader: -1, scope: "org", month: "ALL", access: "all",
                heavy: 40, mod: 10, q: "", band: "", sort: "prompts", dir: -1 };

function readHash() {
  if (!location.hash) return;
  try {
    const p = new URLSearchParams(location.hash.slice(1));
    for (const k of ["leader","scope","month","access","heavy","mod","q","band","sort","dir"]) {
      if (p.has(k)) state[k] = ["leader","heavy","mod","dir"].includes(k) ? +p.get(k) : p.get(k);
    }
  } catch (e) { /* ignore */ }
}
let HASH_WRITING = false;
function writeHash() {
  const p = new URLSearchParams();
  Object.entries(state).forEach(([k, v]) => { if (v !== "" && v != null) p.set(k, v); });
  // Suppress the hashchange we are about to cause, so it cannot loop back into render().
  HASH_WRITING = true;
  try { location.hash = p.toString(); } catch (e) { /* sandboxed */ }
  setTimeout(() => { HASH_WRITING = false; }, 0);
}

// ---------- scope ----------
function scopeUsers() {
  let ids;
  if (state.leader < 0) ids = U.map((_, i) => i);
  else {
    const m = MGRS[String(state.leader)];
    ids = m ? (state.scope === "direct" ? m.d : m.o).slice() : [];
  }
  let list = ids.map(i => U[i]);
  if (state.access === "lic") list = list.filter(u => u.lic);
  else if (state.access === "chat") list = list.filter(u => u.chatOnly);
  return list;
}
const monthsInWindow = () => state.month === "ALL" ? Math.max(1, MONTHS.length) : 1;
const promptsOf = u => state.month === "ALL"
  ? Object.values(u.mo).reduce((a, b) => a + b, 0)
  : (u.mo[state.month] || 0);

function bandOf(u) {
  const perMo = promptsOf(u) / monthsInWindow();
  if (perMo >= state.heavy) return "Heavy";
  if (perMo >= state.mod)   return "Moderate";
  if (promptsOf(u) > 0)     return "Light";
  return "Dormant";
}

// ---------- render ----------
function render() {
  const scoped = scopeUsers();
  scoped.forEach(u => { u.prompts = promptsOf(u); u.band = bandOf(u);
                        u.permo = +(u.prompts / monthsInWindow()).toFixed(1); });

  const lic = scoped.filter(u => u.lic);
  const licActive = lic.filter(u => u.prompts > 0);
  const active = scoped.filter(u => u.prompts > 0);
  const chatOnly = scoped.filter(u => u.chatOnly);
  const chatActive = chatOnly.filter(u => u.prompts > 0);
  const total = scoped.reduce((a, u) => a + u.prompts, 0);
  const heavy = scoped.filter(u => u.band === "Heavy");
  const adoption = lic.length ? Math.round(1000 * licActive.length / lic.length) / 10 : 0;

  // trend delta
  let delta = null;
  if (state.month !== "ALL") {
    const i = MONTHS.indexOf(state.month);
    if (i > 0) {
      const prev = scoped.reduce((a, u) => a + (u.mo[MONTHS[i-1]] || 0), 0);
      if (prev > 0) delta = Math.round(1000 * (total - prev) / prev) / 10;
    }
  }
  const dtxt = delta === null ? "" :
    `<span style="color:${delta >= 0 ? 'var(--cp-success)' : 'var(--cp-danger)'}">
       ${delta >= 0 ? '▲' : '▼'} ${Math.abs(delta)}% vs prior month</span>`;

  el("kpis").innerHTML = [
    ["People in scope", scoped.length, `${lic.length} licensed · ${chatOnly.length} chat-only`],
    ["Licence adoption", adoption + "%", `${licActive.length} of ${lic.length} licensed active`],
    ["Active users", active.length, `${scoped.length - active.length} not using Copilot`],
    ["Total prompts", total.toLocaleString(), dtxt || `${monthsInWindow()} month window`],
    ["Prompts / active user", active.length ? Math.round(10 * total / active.length) / 10 : 0,
      `heavy users: ${heavy.length}`],
    ["Unused licences", lic.length - licActive.length,
      lic.length ? Math.round(100 * (lic.length - licActive.length) / lic.length) + "% of licences" : "—"],
    ["Copilot Chat active", chatActive.length, "unlicensed but engaged"],
  ].map(([k, v, d]) => `<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d}</div></div>`).join("");

  drawTrend(scoped);
  drawSurfaces(scoped);
  drawBands(scoped);
  drawTable(scoped);
  writeHash();
}

function drawTrend(scoped) {
  const w = 640, h = 210, pad = { l: 42, r: 42, t: 14, b: 26 };
  const vals = MONTHS.map(m => scoped.reduce((a, u) => a + (u.mo[m] || 0), 0));
  const act  = MONTHS.map(m => scoped.filter(u => (u.mo[m] || 0) > 0).length);
  const maxV = Math.max(1, ...vals), maxA = Math.max(1, ...act);
  const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
  const bw = iw / Math.max(1, MONTHS.length);

  let s = `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="Monthly prompt volume">`;
  [0, .5, 1].forEach(f => {
    const y = pad.t + ih * (1 - f);
    s += `<line x1="${pad.l}" y1="${y}" x2="${w - pad.r}" y2="${y}" stroke="var(--cp-border)" stroke-width="1"/>`;
    s += `<text x="${pad.l - 6}" y="${y + 4}" text-anchor="end" font-size="9.5" fill="var(--cp-text-soft)">${Math.round(maxV * f)}</text>`;
  });
  MONTHS.forEach((m, i) => {
    const bh = ih * vals[i] / maxV;
    const x = pad.l + i * bw + bw * 0.18, y = pad.t + ih - bh;
    const sel = (state.month === m);
    s += `<rect x="${x}" y="${y}" width="${bw * 0.64}" height="${Math.max(0, bh)}" rx="3"
           fill="var(--cp-accent)" opacity="${sel ? 1 : 0.72}"><title>${m}: ${vals[i]} prompts, ${act[i]} active</title></rect>`;
    if (i % Math.ceil(MONTHS.length / 8) === 0 || MONTHS.length <= 12)
      s += `<text x="${pad.l + i * bw + bw / 2}" y="${h - 8}" text-anchor="middle" font-size="9" fill="var(--cp-text-soft)">${m.slice(2)}</text>`;
  });
  const pts = act.map((a, i) => `${pad.l + i * bw + bw / 2},${pad.t + ih - ih * a / maxA}`).join(" ");
  s += `<polyline points="${pts}" fill="none" stroke="var(--cp-link)" stroke-width="2" stroke-linejoin="round"/>`;
  act.forEach((a, i) => {
    s += `<circle cx="${pad.l + i * bw + bw / 2}" cy="${pad.t + ih - ih * a / maxA}" r="2.6" fill="var(--cp-link)"><title>${MONTHS[i]}: ${a} active users</title></circle>`;
  });
  [0, 1].forEach(f => {
    const y = pad.t + ih * (1 - f);
    s += `<text x="${w - pad.r + 6}" y="${y + 4}" font-size="9.5" fill="var(--cp-link)">${Math.round(maxA * f)}</text>`;
  });
  s += `</svg>`;
  el("trend").innerHTML = s;
}

function drawSurfaces(scoped) {
  const agg = {};
  scoped.forEach(u => {
    const src = state.month === "ALL" ? u.ap : (u.uma[state.month] || {});
    Object.entries(src).forEach(([a, c]) => agg[a] = (agg[a] || 0) + c);
  });
  const rows = Object.entries(agg).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...rows.map(r => r[1]));
  const tot = rows.reduce((a, r) => a + r[1], 0) || 1;
  el("surfaces").innerHTML = rows.length ? rows.map(([a, c]) =>
    `<div class="bar-row"><span title="${esc(a)}">${esc(a)}</span>
      <span class="bar-bg"><span class="bar" style="width:${Math.max(2, 100 * c / max)}%"></span></span>
      <span class="num muted">${Math.round(100 * c / tot)}%</span></div>`).join("")
    : '<div class="muted">No prompts in this scope / period.</div>';
}

const BANDS = ["Heavy", "Moderate", "Light", "Dormant"];
function drawBands(scoped) {
  const counts = {}; BANDS.forEach(b => counts[b] = 0);
  scoped.forEach(u => counts[u.band]++);
  el("bands").innerHTML = BANDS.map(b =>
    `<button class="ghost ${state.band === b ? "on" : ""}" data-band="${b}" style="margin-right:8px">
       ${b}: <strong>${counts[b]}</strong></button>`).join("") +
    (state.band ? `<button class="ghost" data-band="" style="margin-left:4px">Clear filter</button>` : "");
  el("bands").querySelectorAll("button").forEach(btn =>
    btn.addEventListener("click", () => { state.band = btn.dataset.band; render(); }));
}

function spark(u) {
  const v = MONTHS.map(m => u.mo[m] || 0), max = Math.max(1, ...v);
  const w = 84, h = 20, bw = w / Math.max(1, v.length);
  return `<svg width="${w}" height="${h}" role="img" aria-label="prompt trend">` +
    v.map((x, i) => {
      const bh = Math.max(x > 0 ? 1.5 : 0, h * x / max);
      return `<rect x="${i * bw}" y="${h - bh}" width="${Math.max(1, bw - 1)}" height="${bh}" fill="var(--cp-accent)" opacity=".85"/>`;
    }).join("") + `</svg>`;
}

let VIEW = [];
function drawTable(scoped) {
  const q = state.q.trim().toLowerCase();
  VIEW = scoped.filter(u =>
    (!state.band || u.band === state.band) &&
    (!q || u.n.toLowerCase().includes(q) || u.u.toLowerCase().includes(q)));
  const k = state.sort, d = state.dir;
  VIEW.sort((a, b) => {
    const x = a[k], y = b[k];
    if (typeof x === "number" && typeof y === "number") return (x - y) * d;
    return String(x || "").localeCompare(String(y || "")) * d;
  });
  el("rowcount").textContent = `— ${VIEW.length} of ${scoped.length} shown`;
  const cls = { Heavy: "t-heavy", Moderate: "t-moderate", Light: "t-light", Dormant: "t-dormant" };
  const acls = { "Licensed": "t-lic", "Copilot Chat": "t-chat", "No Copilot": "t-none" };
  el("tbl").querySelector("tbody").innerHTML = VIEW.map(u => {
    const top = Object.entries(state.month === "ALL" ? u.ap : (u.uma[state.month] || {}))
      .sort((a, b) => b[1] - a[1])[0];
    return `<tr>
      <td><div>${esc(u.n)}</div><div class="pathcell">${esc(u.u)}</div></td>
      <td>${esc(u.mgrName)}<div class="pathcell">${esc(u.path)}</div></td>
      <td><span class="tag ${acls[u.access]}">${u.access}</span></td>
      <td><span class="tag ${cls[u.band]}">${u.band}</span></td>
      <td class="num">${u.prompts}</td>
      <td class="num">${u.permo}</td>
      <td class="num">${u.days}</td>
      <td>${top ? esc(top[0]) : '<span class="muted">—</span>'}</td>
      <td>${u.l ? esc(u.l) : '<span class="muted">never</span>'}</td>
      <td>${spark(u)}</td></tr>`;
  }).join("") || `<tr><td colspan="10" class="muted" style="padding:18px">No people match these filters.</td></tr>`;
}

// ---------- CSV ----------
function buildCsv() {
  const head = ["Person", "Email", "Manager", "Reports up through", "Access", "Band",
                "Prompts", "Prompts per month", "Active days", "Last used"].concat(MONTHS);
  const q = s => `"${String(s == null ? "" : s).replace(/"/g, '""')}"`;
  return [head.map(q).join(",")].concat(VIEW.map(u => [
    u.n, u.u, u.mgrName, u.path, u.access, u.band, u.prompts, u.permo, u.days, u.l
  ].concat(MONTHS.map(m => u.mo[m] || 0)).map(q).join(","))).join("\r\n");
}

// ---------- events ----------
el("selLeader").addEventListener("change", e => { state.leader = +e.target.value; render(); });
el("selScope").addEventListener("change", e => { state.scope = e.target.value; render(); });
el("selMonth").addEventListener("change", e => { state.month = e.target.value; render(); });
el("selAccess").addEventListener("change", e => { state.access = e.target.value; render(); });
el("inHeavy").addEventListener("input", e => { state.heavy = +e.target.value || 1; render(); });
el("inMod").addEventListener("input", e => { state.mod = +e.target.value || 1; render(); });
el("inSearch").addEventListener("input", e => { state.q = e.target.value; render(); });
el("btnPrint").addEventListener("click", () => window.print());
el("btnCsv").addEventListener("click", () => {
  const csv = buildCsv(), ta = el("csvOut");
  ta.classList.remove("hide"); ta.value = csv; ta.focus(); ta.select();
  try {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "copilot-adoption.csv";
    document.body.appendChild(a); a.click(); a.remove();
  } catch (e) { /* sandbox: textarea fallback is already shown */ }
});
el("tbl").querySelectorAll("th[data-k]").forEach(th =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    state.dir = (state.sort === k) ? -state.dir : (["n","mgrName","access","band","top","l"].includes(k) ? 1 : -1);
    state.sort = k; render();
  }));

el("method").innerHTML =
  "<strong>Method.</strong> Prompt counts come from Microsoft Graph " +
  "<code>interactionHistory/getAllEnterpriseInteractions</code> (<code>interactionType = userPrompt</code>) — " +
  "the only Microsoft surface that exposes per-user prompt volume. Organisation scoping is built by resolving " +
  "<code>/users/{id}/manager</code> transitively, so <em>any</em> leader can be selected, not just top-level executives. " +
  "Licence state comes from assigned Copilot SKUs; “Copilot Chat” means an unlicensed person who is still prompting on a chat surface. " +
  "<strong>Prompt content is never collected</strong> — only counts, timestamps and the app surface.";

// ---------- init ----------
function syncControls() {
  // A select silently ignores an unknown value, so fall back to the tenant view.
  const sl = el("selLeader");
  sl.value = String(state.leader);
  if (sl.selectedIndex < 0) { sl.value = "-1"; state.leader = -1; }
  el("selScope").value = state.scope;
  el("selMonth").value = state.month;
  el("selAccess").value = state.access;
  el("inHeavy").value = state.heavy;
  el("inMod").value = state.mod;
  el("inSearch").value = state.q;
}

readHash();
syncControls();
render();

// Changing only the fragment does not reload the document, so a deep link pasted
// into an already-open tab must be picked up here.
window.addEventListener("hashchange", () => {
  if (HASH_WRITING) return;
  readHash();
  syncControls();
  render();
});
</script>
</body>
</html>
"""


def write_html(p: dict) -> pathlib.Path:
    path = OUT / "copilot-adoption-explorer.html"
    blob = json.dumps(p, separators=(",", ":")).replace("</", "<\\/")
    path.write_text(HTML.replace("__DATA_JSON__", blob), encoding="utf-8")
    return path


if __name__ == "__main__":
    payload = build_payload()
    h = write_html(payload)
    x = write_xlsx(payload)
    c = write_csv(payload)
    print(f"users        : {len(payload['users'])}")
    print(f"leaders      : {len(payload['mgrs'])}")
    print(f"months       : {len(payload['meta']['months'])}")
    print(f"html         : {h}  ({h.stat().st_size/1024:.0f} KB)")
    print(f"xlsx         : {x}")
    print(f"csv          : {c}")
