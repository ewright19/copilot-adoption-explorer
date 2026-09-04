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
   `/beta/admin/reportSettings` and turns it off on each run — this is a **tenant-wide setting
   change**, so get the customer's agreement first.
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

## Layout

```
copilot-adoption-explorer/
  run.py                 one-command collect + build
  config/app.json        tenant, client id, secret   (gitignored)
  src/bootstrap.py       device-code -> app reg + consent + secret
  src/graph_client.py    token cache, throttle-aware retry, paging, $batch
  src/collect.py         Graph -> SQLite
  src/build_report.py    SQLite -> HTML + XLSX + CSV
  src/diagnose.py        tenant readiness check
  out/                   generated artefacts (gitignored)
```
