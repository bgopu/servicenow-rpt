# CDS ROR — ServiceNow Incident Reporting

Automated incident reporting and alerting for the CDS ROR team.  
Pulls ServiceNow incident data, generates HTML reports, and sends emails via Outlook.

## ✨ Features

- **📧 Weekly Report Email**: Sends a formatted HTML incident summary email with YoY comparison
- **🚨 New Incident Alerts**: Detects New-state incidents from the past N days and emails the team
- **👤 WW On-Call Lookup**: Fetches the L3 on-call owner per work week from the Intel Wiki roster
- **📊 YoY Analytics**: Compares current year vs. prior year incident counts and projections
- **🗂️ WW-Grouped Alerts**: Alert emails group incidents by work week with the correct on-call owner per WW

---

## 🖥️ Prerequisites

- Python 3.11 or higher — [python.org](https://www.python.org/downloads/)
- Git — [git-scm.com](https://git-scm.com)
- Microsoft Outlook (desktop app, signed in with your Intel account)
- Intel network or VPN access (for Wiki on-call roster fetch)

---

## 🚀 Setup

### 1. Clone the repository

```bash
git clone https://github.com/bgopu/servicenow-rpt.git
cd servicenow-rpt
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

Then install and register pywin32 (required for Outlook email sending):

```bash
pip install pywin32
python .venv\Scripts\pywin32_postinstall.py -install
```

### 4. Configure recipients

Create `config_sharepoint.json` in the project root (this file is gitignored):

```json
{
  "email": {
    "recipients": ["your.name@intel.com", "teammate@intel.com"]
  }
}
```

### 5. Add the incidents CSV

Export incidents from ServiceNow as CSV and place it at:

```
reports/Incidents_list.csv
```

Required columns: `number`, `short_description`, `priority`, `state`, `assignment_group`, `assigned_to`, `opened_at`

---

## ▶️ Running

### Weekly incident report email

Generates and sends the full weekly HTML report with YoY comparison:

```bash
python servicenow_downloader.py
```

### New-state incident alert email

Scans for New-state incidents in the past 8 days, looks up the on-call owner per WW from the Wiki roster, and sends an alert:

```bash
python alert_new_incidents.py
```

**Options:**

```bash
# Change the look-back window (default: 8 days)
python alert_new_incidents.py --days 14

# Skip the Wiki on-call roster fetch
python alert_new_incidents.py --no-wiki
```

---

## 📁 Project Structure

```
servicenow-rpt/
├── main.py                  # HTML report generator (local file output)
├── servicenow_downloader.py # Downloads CSV + triggers weekly report email
├── send_email.py            # Builds and sends the weekly report email
├── alert_new_incidents.py   # New-state incident alert email with WW on-call
├── incident_analyzer.py     # Incident analytics and statistics
├── report_generator.py      # HTML report template engine
├── requirements.txt         # Python dependencies
├── config_sharepoint.json   # Recipient config (gitignored — create locally)
├── README.md
└── reports/
    ├── Incidents_list.csv   # Current incident export (place here)
    └── 2025_data.csv        # Prior year data for YoY comparison
```

---

## 🔒 Security Notes

- `config_sharepoint.json` is gitignored — never commit it
- Session files (`.servicenow_session.json`, `.wiki_session.json`, etc.) are gitignored
- Outlook sends email using your signed-in identity — no credentials are stored

---

## 🐛 Troubleshooting

| Problem | Fix |
|---|---|
| `❌ pywin32 not installed` | Run `pip install pywin32` then re-register with `pywin32_postinstall.py -install` |
| `❌ No recipients in config_sharepoint.json` | Create the file with an `email.recipients` list |
| `⚠️ Wiki fetch failed` | Check Intel VPN / network, or run with `--no-wiki` |
| `❌ Incidents_list.csv not found` | Export from ServiceNow and place at `reports/Incidents_list.csv` |
| Outlook not sending | Make sure Outlook desktop is open and signed in before running |

---

**Maintained by CDS ROR team**
