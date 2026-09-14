# Copilot Adoption Explorer — Quick Start Guide

> **Disclaimer:** This repository is an example for demonstration and evaluation purposes only.
> It is not a Microsoft product, service, or official offering, and is not built, endorsed,
> warranted, or supported by Microsoft. It is provided **"as is"** without warranties of any
> kind. Use it at your own risk. You are responsible for evaluating and securing the code,
> validating its outputs, and meeting all applicable privacy, security, legal, regulatory, and
> employment requirements before using it in your environment. Do not represent it as a
> Microsoft-supported solution.

**Get user-level Copilot adoption reporting scoped to any leader's organization in under 15 minutes.**

---

## What This Tool Does

The **Copilot Adoption Explorer** exports real user-level Copilot activity (prompts, licensed users, and Copilot Chat usage) for any leader's team—filling a gap left by the native Viva Insights Copilot Dashboard (aggregate only) and M365 Admin Center report (no prompt counts).

**Outputs:**
- 📊 Interactive HTML dashboard (sandbox-safe, works offline)
- 📈 6-sheet Excel workbook (pivot-friendly)
- 📄 CSV exports (for PowerBI, Tableau, etc.)

**Scopes to:**
- Any manager or leader
- Their direct reports
- Licensed AND Copilot Chat users

---

## Prerequisites

**Before you start, you need:**

1. ✅ **Microsoft 365 admin access** (or Global Admin)
   - App registration permission in Azure AD
   - Report access to Graph API interaction history

2. ✅ **Python 3.9+** on your machine
   - Check: `python --version`
   - Install: https://www.python.org/downloads/ (add to PATH during install)

3. ✅ **Git** (optional, for cloning)
   - Or download ZIP: https://github.com/ewright19/copilot-adoption-explorer/archive/refs/heads/main.zip

4. ✅ **Leader's Azure AD UPN**
   - Example: `manager@company.onmicrosoft.com`

---

## Quick Setup (5 min)

### **Step 1: Get the code**

**Option A: Clone (recommended)**
```bash
git clone https://github.com/ewright19/copilot-adoption-explorer.git
cd copilot-adoption-explorer
```

**Option B: Download ZIP**
- Go to: https://github.com/ewright19/copilot-adoption-explorer
- Click Code → Download ZIP
- Unzip and open the folder

### **Step 2: Run bootstrap** (1-time setup)

Bootstrap creates an app registration in your Azure tenant and obtains credentials.

```bash
python src/bootstrap.py YOUR_TENANT_ID
```

**Find your tenant ID:**
1. Go to: https://portal.azure.com
2. Search for "Azure Active Directory"
3. Copy the **Tenant ID** (under "Tenant information")

**During bootstrap:**
- A browser will open asking you to consent to app permissions
- ✅ Review and click **Accept**
- The tool saves credentials locally (never leaves your machine)

### **Step 3: Run preflight validation** (1 min)

Preflight checks permissions and tenant readiness before collecting data.

```bash
python src/preflight.py
```

**Output:**
- ✅ Python environment
- ✅ Dependencies installed
- ✅ Credentials loaded
- ✅ Graph API permissions active
- ✅ Prompt history accessible

If all checks pass, proceed to Step 4.

### **Step 4: Collect + Build** (5–10 min)

This does the heavy lifting: collects from Graph, builds reports.

```bash
python run.py
```

After collection, select the leader in the dashboard. It defaults to **direct reports only** for
that leader; you can switch scope to include the leader's full downstream organization. For
server-enforced manager access, follow the secured web app setup in `INSTALL.md` (tenant admins
with Global Administrator / Global Reader / Reports Reader / Usage Summary Reports Reader get an
automatic full-tenant view on sign-in).

**Output files created in `out/`:**
- `copilot-adoption-explorer.html` — Interactive dashboard (open in browser)
- `copilot-adoption.xlsx` — Excel workbook
- `copilot-user-detail.csv` — Raw data

---

## Use the Dashboard

### **Open the report:**
```bash
# Windows
start out/copilot-adoption-explorer.html

# macOS
open out/copilot-adoption-explorer.html

# Linux
xdg-open out/copilot-adoption-explorer.html
```

### **Dashboard controls:**

| Control | Purpose |
|-----|---------|
| **Leader / organisation** | Pick tenant-wide view or a specific leader |
| **Scope** | For a selected leader, choose direct reports only (default) or full org |
| **Period** | All months or a specific month |
| **Population** | Licensed + Chat, Licensed only, or Copilot Chat only |
| **Search / Sort / Bands** | Slice the table and KPI view quickly |

### **Filter & export:**
- Click column headers to sort
- Use search boxes to filter by name or org
- Right-click the chart → Export as image
- Copy/paste tables into PowerPoint or Confluence

---

## Common Tasks

### **Report for a different leader**
```bash
python run.py
```
Then choose the leader in the dashboard.

### **Include disabled users**
```bash
python run.py --include-disabled
```

### **Faster collection (fewer parallel threads)**
```bash
python run.py --workers 2
```

### **Rebuild reports from cached data** (no Graph call)
```bash
python run.py --build-only
```

---

## Troubleshooting

### **"Permission denied" during bootstrap**
- Ensure you're a **Global Admin** or have **App Registration** permission
- Consent page stuck? Clear browser cache and retry

### **"Slow collection" (5+ min)**
- First run always takes longer (caches data in SQLite)
- If stuck > 10 min: press Ctrl+C, check console output
- Retry with fewer workers: `--workers 2`

### **"No users found" in report**
- Verify the leader's UPN is correct
- Check the leader actually has direct reports
- Ensure Copilot Chat/M365 Copilot is licensed in your tenant

### **"Report anonymization is ON"**
- The tool temporarily disables "display concealed names" to show user names
- This is tenant-wide; discuss with your admin before running
- See INSTALL.md for details

---

## File Locations

After running, your files are:

```
copilot-adoption-explorer/
├── out/
│   ├── copilot-adoption-explorer.html  ← Open this in browser
│   ├── copilot-adoption.xlsx
│   ├── copilot-user-detail.csv
│   └── copilot.db                      ← Local data cache
├── config/
│   └── app.json                 ← Your credentials (keep safe!)
├── src/
├── run.py
└── INSTALL.md                   ← Full admin guide
```

⚠️ **Keep `config/app.json` secure** — it contains a client secret valid for 24 months.

**On shared machines, restrict file permissions:**
```bash
# Windows PowerShell
icacls config/app.json /grant:r "%USERNAME%:F" /inheritance:r

# Linux/macOS
chmod 600 config/app.json
```

**Never commit it to source control** — it is already in `.gitignore` but stay vigilant.

---

## Share the Report

### **Email the HTML file:**
- Attach `out/report.html`
- Recipients can open it in any browser (no server needed)
- No external dependencies — works offline

### **Host on SharePoint:**
1. Upload `report.html` to a SharePoint folder
2. Share the folder link with stakeholders
3. Reports auto-update each time you re-run the tool

### **Export for PowerPoint:**
1. Open the dashboard
2. Use browser DevTools (F12) to copy chart/table HTML
3. Paste into PowerPoint as HTML table

---

## What Data Is Collected?

The tool reads:
- ✅ **User identity** (UPN, display name)
- ✅ **Copilot license status** (from Microsoft 365 licensing API)
- ✅ **Prompt activity** (count, last-active date from Graph interaction history)
- ✅ **Org hierarchy** (manager, direct reports via Azure AD)

It does **NOT**:
- ❌ Inspect prompt content
- ❌ Track keystrokes or browser activity
- ❌ Send data to external services
- ❌ Store passwords or secrets in reports

### ⚠️ Data Security

**The SQLite database (`out/copilot.db`) and Excel workbook accumulate user-level data.** Treat them with the same care as HR or personnel reports:

- **Local use only** — Do not store unencrypted on shared machines or cloud without encryption
- **Access control** — Restrict to approved users only (via file permissions or folder sharing)
- **Production deployments** — Use Azure Automation with managed identity or encrypt at rest
- **Retention** — Delete old reports when no longer needed

---

## Next Steps

✅ **Done?** Share the report with your stakeholders!

📖 **Need more detail?** See `INSTALL.md` for:
- Full permission reference
- Tenant readiness checklist
- Production deployment guidance
- Credential rotation for long-term use

❓ **Questions?** Check the GitHub Issues tab:
https://github.com/ewright19/copilot-adoption-explorer/issues

---

**Tool Repository:** https://github.com/ewright19/copilot-adoption-explorer

**Version:** 1.0 | Last Updated: September 2026
