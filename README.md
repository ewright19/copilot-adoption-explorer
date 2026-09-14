# Copilot Adoption Explorer

User-level Microsoft 365 Copilot adoption and **prompt-volume** reporting, scoped to **any
leader's organisation** — not just top-level executives.

Built to close the gap the native tooling leaves:

| Need | Viva Insights Copilot Dashboard | M365 Admin Center report | **This tool** |
|---|---|---|---|
| Per-user detail | ✗ aggregate only | ✓ | ✓ |
| **Prompt counts** | ✗ | ✗ (last-activity dates only) | ✓ |
| Scope to *any* manager's org | ✗ (top-level execs only) | ✗ | ✓ |
| Copilot Chat (unlicensed) users | partial | ✗ | ✓ |
| Manager-filterable, shareable export | ✗ | CSV dump only | ✓ HTML + XLSX + CSV |

---

## What it produces

1. **`copilot-adoption-explorer.html`** — a single self-contained file. Pick a leader, choose
   *direct reports* vs *entire organisation*, filter by month, licence type, or engagement band,
   search, sort, and export the current view to CSV. No server, no network calls, no install.
   Safe to email to a customer or drop in SharePoint/OneDrive.
2. **`copilot-adoption.xlsx`** — 6 sheets: Summary, By leader, User detail, Monthly trend,
   Surfaces, Methodology.
3. **`copilot-user-detail.csv`** — flat per-user extract for Power BI or Excel modelling.
4. **`copilot.db`** — SQLite snapshot store. Re-running is incremental and idempotent, so month
   over month history accumulates even though Graph only exposes a rolling window.

---

## How it works

| Signal | Graph endpoint | Why |
|---|---|---|
| **Prompt counts** (the engine) | `/beta/copilot/users/{id}/interactionHistory/getAllEnterpriseInteractions` | The *only* Microsoft surface that exposes per-user prompt volume, per app surface, per timestamp. |
| Org hierarchy | `/v1.0/users/{id}/manager` (batched, resolved transitively) | Lets you roll up to **any** leader, at any depth. |
| Licences | `/v1.0/subscribedSkus`, `/v1.0/users/{id}/licenseDetails` | Licensed vs Copilot-Chat-only; unused-seat counting. |
| Corroboration | `/beta/reports/getMicrosoft365CopilotUsageUserDetail` | Stored as a cross-check. Note it returns *last-activity dates only* — never counts. |

Engagement bands are computed from prompts-per-month and are adjustable live in the dashboard:
**Heavy** ≥40 · **Moderate** ≥10 · **Light** ≥1 · **Dormant** 0.

### Privacy
Only **counts, timestamps and app surface** are collected. **Prompt and response content is never
read or stored.** State this explicitly to customers — some jurisdictions and works councils
require notification before per-user productivity reporting.

---

## Setup

### Prerequisites
* Python 3.9+ — `pip install requests openpyxl`
* A Global Administrator (or Privileged Role Admin + Application Admin) for one-time consent.

### 1. Bootstrap the app registration

```powershell
python src\bootstrap.py <tenant-id>
```

Device-code sign-in, then it creates the app registration, grants and consents the permissions,
mints a client secret, and writes `config/app.json`.

**Application (app-only) permissions required — all need admin consent:**

| Permission | Purpose |
|---|---|
| `AiEnterpriseInteraction.Read.All` | Prompt-level interaction history |
| `User.Read.All` | Directory + manager hierarchy |
| `Organization.Read.All` | Subscribed SKUs / seat counts |
| `Reports.Read.All` | Copilot usage report |
| `ReportSettings.ReadWrite.All` | Turn off name concealment (see gotcha below) |

> **`AiEnterpriseInteraction.Read.All` must be the *application* permission.** The delegated
> variant only ever returns the signed-in user's own interactions — it cannot report on an org.

### 2. Collect and build

```powershell
python run.py                  # collect from Graph, then build everything
python run.py --build-only     # rebuild deliverables from the existing snapshot
python run.py --workers 24     # raise concurrency for large tenants
python run.py --include-disabled
```

### 3. Health check a new tenant before committing

```powershell
python src\diagnose.py
```

Reports concealment state, SKU/seat counts, whether the usage report returns usable rows, and
whether interaction history is flowing.

---

## Gotchas found in real tenants

1. **"Display concealed user names" defaults to ON.** When on, Graph returns hashed UPNs and
   *every* user-level report is silently useless. The collector checks
   `/beta/admin/reportSettings`. If concealment is enabled, run `preflight.py` and explicitly
   approve the **tenant-wide setting change** before collection; collection itself never changes it.
2. **The Copilot usage report gives dates, not counts** — and in some tenants the activity-date
   columns come back blank entirely. Never build adoption metrics on it alone.
3. **Directory attributes are often empty.** `department` / `jobTitle` / `officeLocation` /
   `companyName` were 0% populated in the test tenant, which is exactly why manager-hierarchy
   rollup (rather than `prepare-org-data` attribute grouping) is the reliable scoping dimension.
   The schema and exports already carry these fields for tenants where they *are* populated.
4. **`$expand=manager` on the users collection is unreliable** at scale — the collector uses
   `$batch` against `/users/{id}/manager` instead.
5. **Interaction history is retained per tenant retention/audit policy.** Run monthly and let the
   SQLite store accumulate history rather than assuming Graph will backfill.

---

## Scaling

The per-user interaction pull is the expensive call — cost grows linearly with headcount, not
tenant size. The test tenant (34 users, ~3.3k interactions) collects in ~65s at 8 workers.

* **< 1,000 users** — defaults are fine.
* **1,000–10,000** — `--workers 24`, run off-hours. The client already honours `Retry-After` on
  429/503 with exponential backoff.
* **> 10,000** — scope to the leader's subtree rather than the whole tenant, and keep runs
  incremental (the `INSERT OR IGNORE` dedupe makes re-runs cheap).

## Scheduling monthly

Windows Task Scheduler:

```powershell
schtasks /create /tn "Copilot Adoption Explorer" /sc monthly /d 1 /st 03:00 ^
  /tr "python \"<path>\copilot-adoption-explorer\run.py\""
```

For unattended production use, replace the client secret with a **certificate credential** or
managed identity and host in Azure Automation / an Azure Function.

---

## Secured web app (real, server-enforced RBAC)

The static HTML/XLSX/CSV export above is a **single self-contained file** — anyone who has the
file can see everything in it via View Source, so it should only be shared with people who are
already allowed to see the whole dataset.

If you need a leader (e.g. Amber) and her delegates to sign in and see **only her org**, with the
server enforcing that boundary (not a client-side toggle that could be bypassed), use `webapp/app.py`
instead. It requires hosting (it is a running service, not an emailable file) but gives real
per-person access control:

* Each visitor signs in with their own Microsoft 365 account (Entra ID / MSAL, authorization-code
  flow).
* A leader automatically sees their own org — this needs **no configuration**, it falls out of the
  manager hierarchy already in the snapshot.
* A **delegate** (someone covering for a leader) sees that leader's org only if they're a member of
  an Azure AD group you configure in `config/access_control.json`.
* An optional **admin group** can be granted the full-tenant view for IT/reporting admins.
* The server computes the viewer's authorized scope and builds a **filtered** payload before
  anything is sent to the browser — a user literally cannot receive another leader's data over the
  wire, so there is nothing to leak via DevTools.

### Setup

```powershell
# 1. If you haven't already, (re-)bootstrap the app-only collector - it now also
#    requests GroupMember.Read.All, used server-side to check delegate group membership.
python src\bootstrap.py <tenant-id-or-domain>

# 2. Register a SEPARATE, delegated sign-in app for the web app itself.
#    Use your real hostname once you know it; localhost is fine for testing.
python src\bootstrap_webapp.py <tenant-id-or-domain> http://localhost:5000/auth/callback

# 3. Configure who can see whom.
copy config\access_control.json.example config\access_control.json
notepad config\access_control.json   # fill in adminGroupId / delegateGroupId as needed

# 4. Install the extra web dependencies and run it.
pip install -r requirements.txt
python webapp\app.py
```

Then browse to `http://localhost:5000/` — you'll be redirected to Microsoft sign-in, and land on a
dashboard scoped to whatever org(s) you're authorized for.

### Deploying it for real

* Run behind HTTPS (Azure App Service, Azure Container Apps, or any host with a TLS certificate) —
  session cookies are marked `Secure` by default and browsers will reject them over plain HTTP.
  For **local testing only**, set `WEBAPP_DEV_INSECURE_COOKIES=1` to allow `http://localhost`.
* Update the redirect URI to your real hostname (re-run `bootstrap_webapp.py`, or add an extra
  redirect URI in Entra admin center → App registrations → your app → Authentication).
* Use a production WSGI server (`waitress`, `gunicorn`, or the platform's built-in one) — the
  Flask dev server printed at startup is not for production traffic.
* `config/webapp.json` and `config/access_control.json` are gitignored and contain secrets/UPNs —
  never commit them; deploy them as app settings / a mounted secret instead.

---

## Layout

```
copilot-adoption-explorer/
  run.py                        one-command collect + build (static export)
  config/app.json                tenant, client id, secret               (gitignored)
  config/webapp.json             web app sign-in credentials             (gitignored)
  config/access_control.json     leader -> delegate/admin group mapping  (gitignored)
  src/bootstrap.py               device-code -> app-only reg + consent + secret
  src/bootstrap_webapp.py        device-code -> delegated web-app reg + secret
  src/graph_client.py            token cache, throttle-aware retry, paging, $batch
  src/collect.py                 Graph -> SQLite
  src/build_report.py            SQLite -> HTML + XLSX + CSV, + server-side scope filtering
  src/diagnose.py                tenant readiness check
  webapp/app.py                  secured, RBAC-scoped Flask dashboard
  webapp/access_control.py       leader self-match / delegate group / admin group resolution
  out/                           generated artefacts (gitignored)
```

