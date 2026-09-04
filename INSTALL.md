# Install guide — Copilot Adoption Explorer

For the **tenant administrator** installing this in your own Microsoft 365 tenant.
Takes about 15 minutes, most of which is waiting for admin consent to propagate.

---

## Before you start

| You need | Why |
|---|---|
| **Global Administrator** (or Privileged Role Admin + Application Administrator) | One-time app registration and admin consent |
| **Python 3.9 or newer** | The collector — [python.org/downloads](https://www.python.org/downloads/). On Windows, tick **"Add python.exe to PATH"** during install. |
| Microsoft 365 Copilot licences assigned | There is nothing to report on otherwise |
| Outbound HTTPS to `login.microsoftonline.com` and `graph.microsoft.com` | Graph API access |

### Two decisions to make first

1. **Report name concealment will be turned off.** M365 Admin Center has a setting,
   *"Display concealed user names in all reports"*, that is **ON by default** in many tenants.
   While on, Microsoft hashes every user name and **no** user-level reporting is possible.
   This tool turns it off. That is a **tenant-wide change affecting all admin reports** —
   get sign-off before you run it.
2. **This produces per-user productivity data.** Only counts, timestamps and the app used are
   collected — **never prompt or response content**. Even so, some regions and works councils
   require employee notification or consultation before per-user reporting. Check with your
   privacy/HR/legal team.

---

## Step 1 — Unpack and install dependencies

```powershell
cd copilot-adoption-explorer
pip install -r requirements.txt
```

## Step 2 — Register the application

Replace with your own tenant ID or domain:

```powershell
python src\bootstrap.py contoso.onmicrosoft.com
```

It prints a device code and a URL. Open the URL, enter the code, and sign in **as a Global
Administrator**. The script then creates the app registration, requests the permissions below,
grants admin consent, creates a client secret, and writes `config/app.json`.

**Application (app-only) permissions requested:**

| Permission | Purpose |
|---|---|
| `AiEnterpriseInteraction.Read.All` | Prompt-level interaction history — the core data source |
| `User.Read.All` | Directory and manager hierarchy |
| `Organization.Read.All` | Subscribed SKUs and seat counts |
| `Reports.Read.All` | Copilot usage report (corroborating signal) |
| `ReportSettings.ReadWrite.All` | Turn off report name concealment |

> **These must be *Application* permissions, not Delegated.** The delegated form of
> `AiEnterpriseInteraction.Read.All` only ever returns the signed-in user's own interactions
> and cannot report on an organisation.

If your tenant blocks self-service consent, hand the permission table above to whoever
administers Entra, then re-run bootstrap.

## Step 3 — Verify before collecting

```powershell
python src\preflight.py
```

Checks Python, dependencies, config, token acquisition, each permission with a live call, and
whether prompt history actually flows. Every failure names the fix. Do not continue until you
see `READY`.

## Step 4 — Collect and build

```powershell
python run.py
```

Produces, in `out/`:

| File | Use |
|---|---|
| `copilot-adoption-explorer.html` | The dashboard — one self-contained file, open in any browser |
| `copilot-adoption.xlsx` | 6-sheet workbook for analysts |
| `copilot-user-detail.csv` | Flat extract for Power BI |
| `copilot.db` | SQLite history store — **keep this**, it accumulates month over month |

Options:

```powershell
python run.py --workers 24        # large tenants
python run.py --build-only        # rebuild reports without re-querying Graph
python run.py --include-disabled  # include disabled accounts
```

---

## Step 5 — Share with managers

`copilot-adoption-explorer.html` is a single file with no external dependencies. Email it, or
drop it in SharePoint/OneDrive — it renders correctly in the in-browser preview.

In the dashboard, pick a leader, choose **entire organisation** or **direct reports only**, then
filter by month, licence type, or engagement band. The URL updates as you filter, so you can
copy it and send a manager straight to their own view. **Export CSV** exports the current view.

> The HTML embeds the user-level data it displays. Treat it with the same care as any HR report
> and share only with people entitled to see it.

---

## Keeping it current

Run monthly — Graph only exposes a rolling window, but the SQLite store accumulates history:

```powershell
schtasks /create /tn "Copilot Adoption Explorer" /sc monthly /d 1 /st 03:00 ^
  /tr "python \"C:\path\to\copilot-adoption-explorer\run.py\""
```

**The client secret expires.** `bootstrap.py` creates a 24-month secret. For unattended
production use, replace it with a **certificate credential** or run under a managed identity in
Azure Automation. `config/app.json` is plain text — protect it with filesystem ACLs and never
commit it to source control (it is gitignored).

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `NOT READY - AiEnterpriseInteraction.Read.All denied` | Consented as Delegated instead of Application. Fix in Entra > App registrations > API permissions. |
| All user names look like hashes | Report name concealment is on. `run.py` disables it; re-run. |
| Everyone shows 0 prompts | Nobody has used Copilot yet, or licences were assigned very recently. |
| Empty **Manager** column | Manager relationships are not populated in Entra — the org rollup depends on them. |
| `could not acquire a token` | Client secret expired — re-run `bootstrap.py`. |
| HTTP 429s in the log | Normal; the client backs off and retries automatically. Lower `--workers`. |
| Collection is slow | Cost scales with user count. Use `--workers 24` and run off-hours. |
