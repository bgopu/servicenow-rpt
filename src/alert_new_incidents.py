"""
New Incident Alert — Past 8 Days
=================================
1. Reads Incidents_list.csv for State=New incidents opened in the last 8 days
2. Fetches the L3 On-Call roster from Intel Wiki (Playwright + Intel SSO)
   to identify the current on-call owner
3. Sends an alert email via Outlook

Usage:
  python alert_new_incidents.py
  python alert_new_incidents.py --days 3   # look back 3 days instead of 8
  python alert_new_incidents.py --no-wiki  # skip wiki, show "See roster" note
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz

WIKI_ROSTER_URL = "https://wiki.ith.intel.com/spaces/EnterpriseDaaS/pages/3027366462/L3+On-Call+Support+Duty+Roster"
_ROOT = Path(__file__).parent.parent
WIKI_SESSION_FILE = _ROOT / ".wiki_session.json"
SNOW_BASE = "https://intel.service-now.com"
CONFIG_FILE = _ROOT / "config" / "config_sharepoint.json"
CSV_FILE = _ROOT / "reports" / "Incidents_list.csv"

IST = pytz.timezone("Asia/Kolkata")


# ─── helpers ──────────────────────────────────────────────────────────────────

def get_intel_ww(dt: datetime = None) -> int:
    """Return Intel Work Week number for the given datetime (default: now)."""
    if dt is None:
        dt = datetime.now(IST)
    year_start = IST.localize(datetime(dt.year, 1, 1)) if dt.tzinfo else datetime(dt.year, 1, 1)
    days = (dt - year_start).days + 1
    if days < 4:
        return 1
    return ((days - 4) // 7) + 2


def load_recipients() -> list[str]:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        r = cfg.get("email", {}).get("recipients", [])
        return [x.strip() for x in r if x.strip()]
    return []


# ─── Wiki roster fetch ─────────────────────────────────────────────────────────

async def fetch_roster_html(headed: bool = False) -> str | None:
    """
    Use Playwright to fetch the roster wiki page.
    Tries saved session first; if that fails (SSO redirect), opens headed browser.
    Returns raw page HTML or None on failure.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("❌ playwright not installed. Run: pip install playwright && playwright install chromium")
        return None

    async with async_playwright() as p:
        launch_opts = {"headless": not headed}
        browser = await p.chromium.launch(**launch_opts)

        # Try loading saved session (from ServiceNow downloader or previous wiki login)
        storage = None
        for sess_file in [WIKI_SESSION_FILE, _ROOT / ".servicenow_session.json"]:
            if sess_file.exists():
                with open(sess_file) as f:
                    storage = json.load(f)
                break

        ctx_opts = {"storage_state": storage} if storage else {}
        context = await browser.new_context(**ctx_opts)
        page = await context.new_page()

        try:
            await page.goto(WIKI_ROSTER_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)

            url = page.url.lower()
            # If we hit an SSO page, open headed and wait for login
            if any(x in url for x in ["idp.", "/login", "/sso", "microsoftonline", "okta", "ping", "signin"]):
                print("🔐 Intel SSO login required for Wiki. Complete login in the browser window…")
                await browser.close()

                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(WIKI_ROSTER_URL, wait_until="domcontentloaded", timeout=30_000)

                # Wait up to 3 min for user to complete SSO and land on wiki page
                await page.wait_for_function(
                    """() => window.location.href.includes('wiki.ith.intel.com/spaces')""",
                    timeout=180_000,
                )
                await page.wait_for_timeout(2000)

                # Save session for next time
                await context.storage_state(path=str(WIKI_SESSION_FILE))
                print(f"💾 Wiki session saved → {WIKI_SESSION_FILE}")

            html = await page.content()
            await browser.close()
            return html

        except Exception as e:
            print(f"⚠️  Wiki fetch failed: {e}")
            await browser.close()
            return None


def parse_oncall_from_html(html: str) -> str | None:
    """
    Parse the Confluence table to find the current on-call person.
    Handles both WW-based and date-range-based roster tables.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    today = datetime.now(IST)
    current_ww = get_intel_ww(today)
    today_date = today.date()

    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Get header row to understand column positions
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]

        # Find name column — look for common labels
        name_col = next(
            (i for i, h in enumerate(headers) if any(x in h for x in ["name", "owner", "engineer", "person", "who", "duty"])),
            None,
        )
        # Find period column — WW number column
        period_col = next(
            (i for i, h in enumerate(headers) if any(x in h for x in ["ww", "week"])),
            None,
        )
        # Find start date column — used to disambiguate same WW across years
        start_date_col = next(
            (i for i, h in enumerate(headers) if any(x in h for x in ["start", "from", "begin"])),
            None,
        )
        # Fallback: if no WW column found, look for date/period columns
        if period_col is None:
            period_col = next(
                (i for i, h in enumerate(headers) if any(x in h for x in ["date", "period", "when"])),
                None,
            )

        if name_col is None:
            # If no labelled header, try heuristic: first two columns = period, name
            name_col = 1
            period_col = 0

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(name_col, period_col if period_col is not None else 0):
                continue

            period_text = cells[period_col].get_text(separator=" ", strip=True) if period_col is not None else ""
            name_text = cells[name_col].get_text(separator=" ", strip=True)

            if not name_text or not period_text:
                continue

            # ── Try WW matching ──────────────────────────────────────────
            ww_matches = re.findall(r"ww\s*(\d+)", period_text, re.I)
            if ww_matches:
                ww_nums = [int(w) for w in ww_matches]
                ww_range = range(min(ww_nums), max(ww_nums) + 1) if len(ww_nums) > 1 else [ww_nums[0]]
                if current_ww in ww_range:
                    # ── Year guard: verify start date year == current year ──
                    year_ok = True  # default: accept if we can't determine year
                    if start_date_col is not None and start_date_col < len(cells):
                        start_text = cells[start_date_col].get_text(strip=True)
                        # Parse dates like "3/2/2026", "2026-03-02", etc.
                        try:
                            parsed = pd.to_datetime(start_text, errors="coerce")
                            if pd.notna(parsed):
                                year_ok = (parsed.year == today.year)
                        except Exception:
                            pass
                    if year_ok:
                        return name_text

            # ── Try date range matching ──────────────────────────────────
            # Look for patterns like "Mar 2 - Mar 8" or "2026-03-02 to 2026-03-08"
            date_patterns = [
                r"(\w+\s+\d+)\s*[-–to]+\s*(\w+\s+\d+)",       # Mar 2 - Mar 8
                r"(\d{4}-\d{2}-\d{2})\s*[-–to]+\s*(\d{4}-\d{2}-\d{2})",  # ISO dates
                r"(\d{1,2}/\d{1,2})\s*[-–to]+\s*(\d{1,2}/\d{1,2})",      # MM/DD
            ]
            for pat in date_patterns:
                m = re.search(pat, period_text, re.I)
                if m:
                    try:
                        year = today.year
                        d1 = pd.to_datetime(f"{m.group(1)} {year}", errors="coerce")
                        d2 = pd.to_datetime(f"{m.group(2)} {year}", errors="coerce")
                        if pd.notna(d1) and pd.notna(d2):
                            if d1.date() <= today_date <= d2.date():
                                return name_text
                    except Exception:
                        pass

    return None


def parse_oncall_for_ww(html: str, target_ww: int) -> str | None:
    """
    Parse the Confluence roster table and return the on-call owner for a specific target WW.
    Same logic as parse_oncall_from_html but looks up target_ww instead of current WW.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    today = datetime.now(IST)

    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        name_col = next(
            (i for i, h in enumerate(headers) if any(x in h for x in ["name", "owner", "engineer", "person", "who", "duty"])),
            None,
        )
        period_col = next(
            (i for i, h in enumerate(headers) if any(x in h for x in ["ww", "week"])),
            None,
        )
        start_date_col = next(
            (i for i, h in enumerate(headers) if any(x in h for x in ["start", "from", "begin"])),
            None,
        )
        if period_col is None:
            period_col = next(
                (i for i, h in enumerate(headers) if any(x in h for x in ["date", "period", "when"])),
                None,
            )
        if name_col is None:
            name_col = 1
            period_col = 0

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(name_col, period_col if period_col is not None else 0):
                continue

            period_text = cells[period_col].get_text(separator=" ", strip=True) if period_col is not None else ""
            name_text = cells[name_col].get_text(separator=" ", strip=True)
            if not name_text or not period_text:
                continue

            ww_matches = re.findall(r"ww\s*(\d+)", period_text, re.I)
            if ww_matches:
                ww_nums = [int(w) for w in ww_matches]
                ww_range = range(min(ww_nums), max(ww_nums) + 1) if len(ww_nums) > 1 else [ww_nums[0]]
                if target_ww in ww_range:
                    year_ok = True
                    if start_date_col is not None and start_date_col < len(cells):
                        start_text = cells[start_date_col].get_text(strip=True)
                        try:
                            parsed = pd.to_datetime(start_text, errors="coerce")
                            if pd.notna(parsed):
                                year_ok = (parsed.year == today.year)
                        except Exception:
                            pass
                    if year_ok:
                        return name_text

    return None


# ─── Incident filtering ────────────────────────────────────────────────────────

OPEN_STATES = ["new", "in progress", "on hold"]


def current_quarter_start(dt: datetime = None) -> datetime:
    """Return the first day of the current quarter (IST) as a naive datetime."""
    if dt is None:
        dt = datetime.now(IST)
    month = dt.month
    q_start_month = ((month - 1) // 3) * 3 + 1
    return datetime(dt.year, q_start_month, 1)


def get_new_incidents(quarter_only: bool = True) -> pd.DataFrame:
    """Return open incidents (New / In Progress / On Hold) from the CSV."""
    if not CSV_FILE.exists():
        print(f"❌ {CSV_FILE} not found. Run servicenow_downloader.py first.")
        sys.exit(1)

    df = pd.read_csv(CSV_FILE, low_memory=False)
    df = df.dropna(how="all").drop_duplicates()

    date_col = next((c for c in df.columns if "opened" in c.lower()), None)
    state_col = next((c for c in df.columns if "state" in c.lower()), None)
    num_col = next((c for c in df.columns if c.lower() in ["number", "inc"]), None)
    desc_col = next((c for c in df.columns if "short" in c.lower() and "desc" in c.lower()), None)
    ag_col = next((c for c in df.columns if "assignment" in c.lower() and "group" in c.lower()), None)
    pri_col = next((c for c in df.columns if "priority" in c.lower()), None)
    owner_col = next((c for c in df.columns if c.lower() == "assigned_to"), None)

    if date_col:
        df["_opened"] = pd.to_datetime(df[date_col], format="ISO8601", errors="coerce")
    else:
        print("⚠️  No 'opened' date column found — returning all open incidents.")
        df["_opened"] = pd.NaT

    # Apply quarter filter (default) or show all open incidents
    if quarter_only and df["_opened"].notna().any():
        q_start = current_quarter_start()
        df = df[df["_opened"] >= q_start]

    # Filter by open states
    if state_col:
        df = df[df[state_col].str.strip().str.lower().isin(OPEN_STATES)]

    # Sort newest first
    df = df.sort_values("_opened", ascending=False)

    # Select only useful display columns
    keep = [c for c in [num_col, state_col, desc_col, pri_col, ag_col, owner_col, date_col] if c]
    rename_map = {}
    if num_col:   rename_map[num_col]   = "Number"
    if state_col: rename_map[state_col] = "State"
    if desc_col:  rename_map[desc_col]  = "Short Description"
    if pri_col:   rename_map[pri_col]   = "Priority"
    if ag_col:    rename_map[ag_col]    = "Assignment Group"
    if owner_col: rename_map[owner_col] = "Owner"
    if date_col:  rename_map[date_col]  = "Opened"
    return df[keep].rename(columns=rename_map) if keep else df


# ─── Email builder ─────────────────────────────────────────────────────────────

def build_alert_email(incidents_df: pd.DataFrame, roster_html: str | None, quarter_only: bool = True) -> str:
    today = datetime.now(IST)
    ww = get_intel_ww(today)
    now_str = today.strftime("%B %d, %Y at %I:%M %p IST")
    count = len(incidents_df)
    q_start = current_quarter_start(today)
    quarter_num = (today.month - 1) // 3 + 1
    period_label = f"Q{quarter_num} {today.year} (since {q_start.strftime('%b %d')})" if quarter_only else "All Open"

    oncall_name = parse_oncall_from_html(roster_html) if roster_html else None
    oncall_html = (
        f'<span style="font-size:15px;font-weight:700;color:#0068b8;">{oncall_name}</span>'
        if oncall_name
        else '<span style="color:#888;font-style:italic;">Could not determine (check roster)</span>'
    )
    roster_link = f'<a href="{WIKI_ROSTER_URL}" style="color:#0068b8;">L3 On-Call Roster</a>'

    # Build incident table rows grouped by WW
    table_rows = ""
    if not incidents_df.empty:
        def _safe_ww(opened_str):
            try:
                dt = pd.to_datetime(opened_str, errors="coerce")
                if pd.notna(dt):
                    return get_intel_ww(dt)
            except Exception:
                pass
            return ww

        incidents_df = incidents_df.copy()
        incidents_df["_ww"] = incidents_df["Opened"].apply(_safe_ww)

        for ww_num, group in sorted(incidents_df.groupby("_ww"), key=lambda x: x[0], reverse=True):
            ww_oncall = parse_oncall_for_ww(roster_html, ww_num) if roster_html else None
            ww_oncall_str = ww_oncall if ww_oncall else "Not on record"
            table_rows += f"""
        <tr style="background:#e3f2fd;">
          <td colspan="8" style="padding:7px 12px;">
            <span style="font-size:12px;font-weight:700;color:#1565c0;text-transform:uppercase;
                         letter-spacing:0.4px;">WW{ww_num:02d}</span>
            &nbsp;&nbsp;
            <span style="font-size:12px;color:#444;">On-Call: <strong>{ww_oncall_str}</strong></span>
          </td>
        </tr>"""
            for _, row in group.iterrows():
                num = row.get("Number", "")
                snow_url = f"{SNOW_BASE}/nav_to.do?uri=incident.do?sysparm_query=number={num}"
                pri = str(row.get("Priority", ""))
                pri_color = "#c62828" if "2 - High" in pri or "1 - " in pri else ("#e65100" if "3 - Mod" in pri else "#555")
                opened = str(row.get("Opened", ""))[:16]
                try:
                    opened_dt = pd.to_datetime(row.get("Opened", ""), errors="coerce")
                    days_open = (datetime.now() - opened_dt.replace(tzinfo=None)).days if pd.notna(opened_dt) else "-"
                except Exception:
                    days_open = "-"
                if isinstance(days_open, int):
                    if days_open >= 14:
                        days_color = "#c62828"
                    elif days_open >= 7:
                        days_color = "#e65100"
                    else:
                        days_color = "#2e7d32"
                else:
                    days_color = "#888"
                desc = str(row.get("Short Description", ""))[:80]
                ag = str(row.get("Assignment Group", ""))
                assigned = str(row.get("Owner", "")).strip()
                owner = assigned if assigned and assigned.lower() not in ("nan", "none", "") else ww_oncall_str
                state_val = str(row.get("State", "")).strip()
                state_lower = state_val.lower()
                if state_lower == "in progress":
                    state_bg, state_fg = "#fff3e0", "#e65100"
                elif state_lower == "on hold":
                    state_bg, state_fg = "#f3e5f5", "#6a1b9a"
                else:  # new
                    state_bg, state_fg = "#e3f2fd", "#1565c0"
                table_rows += f"""
        <tr style="border-bottom:1px solid #f0f0f0;">
          <td style="padding:8px 10px;font-size:13px;">
            <a href="{snow_url}" target="_blank"
               style="color:#1a73e8;font-weight:700;text-decoration:underline;">{num}</a>
          </td>
          <td style="padding:6px 10px;white-space:nowrap;">
            <span style="background:{state_bg};color:{state_fg};font-size:11px;font-weight:700;
                         padding:2px 7px;border-radius:3px;">{state_val}</span>
          </td>
          <td style="padding:8px 10px;font-size:13px;color:#333;">{desc}</td>
          <td style="padding:8px 10px;font-size:13px;font-weight:700;color:{pri_color};">{pri}</td>
          <td style="padding:8px 10px;font-size:13px;color:#555;">{ag}</td>
          <td style="padding:8px 10px;font-size:13px;color:#444;">{owner}</td>
          <td style="padding:8px 10px;font-size:13px;font-weight:700;color:{days_color};text-align:center;white-space:nowrap;">{days_open}d</td>
          <td style="padding:8px 10px;font-size:12px;color:#888;white-space:nowrap;">{opened}</td>
        </tr>"""
    else:
        table_rows = """
        <tr>
          <td colspan="8" style="padding:20px;text-align:center;color:#4caf50;font-weight:700;">
            ✅ No open incidents
          </td>
        </tr>"""

    status_color = "#c62828" if count > 0 else "#2e7d32"
    status_label = f"{count} Open Incident{'s' if count != 1 else ''}" if count > 0 else "No Open Incidents"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Segoe UI,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;">
<tr><td align="center" style="padding:24px 16px;">

  <table width="620" cellpadding="0" cellspacing="0"
         style="background:#fff;border-radius:6px;border:1px solid #dde3ea;overflow:hidden;">

    <!-- header -->
    <tr>
      <td bgcolor="#c0392b" style="padding:22px 28px;background-color:#c0392b;">
        <p style="margin:0;font-size:19px;font-weight:700;color:#fff;">
          &#9888;&#65039; CDS ROR — Open Incidents
        </p>
        <p style="margin:4px 0 0 0;font-size:13px;color:#ffcdd2;">
          WW{ww:02d} &nbsp;|&nbsp; {period_label} &nbsp;&bull;&nbsp; New &bull; In Progress &bull; On Hold
        </p>
      </td>
    </tr>

    <!-- on-call section -->
    <tr>
      <td style="padding:20px 28px 0 28px;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#e3f2fd;border-left:4px solid #1565c0;border-radius:4px;padding:14px 16px;">
          <tr>
            <td style="padding:14px 16px;">
              <p style="margin:0 0 4px 0;font-size:12px;font-weight:700;color:#1565c0;
                         text-transform:uppercase;letter-spacing:0.5px;">
                &#128100; WW{ww:02d} - On-Call Owner
              </p>
              <p style="margin:0;font-size:15px;">{oncall_html}</p>
              <p style="margin:6px 0 0 0;font-size:12px;color:#666;">
                Source: {roster_link}
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- incident count badge -->
    <tr>
      <td style="padding:18px 28px 10px 28px;">
        <p style="margin:0;font-size:13px;font-weight:700;color:{status_color};">
          &#128204; {status_label} &nbsp;&mdash;&nbsp; {period_label}
        </p>
      </td>
    </tr>

    <!-- incident table -->
    <tr>
      <td style="padding:0 28px 24px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;border:1px solid #e0e0e0;border-radius:4px;overflow:hidden;">
          <thead>
            <tr style="background:#37474f;">
              <th style="padding:9px 10px;text-align:left;font-size:12px;color:#fff;
                          font-weight:700;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;">
                Incident #
              </th>
              <th style="padding:9px 10px;text-align:left;font-size:12px;color:#fff;
                          font-weight:700;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;">
                State
              </th>
              <th style="padding:9px 10px;text-align:left;font-size:12px;color:#fff;
                          font-weight:700;text-transform:uppercase;letter-spacing:0.4px;">
                Description
              </th>
              <th style="padding:9px 10px;text-align:left;font-size:12px;color:#fff;
                          font-weight:700;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;">
                Priority
              </th>
              <th style="padding:9px 10px;text-align:left;font-size:12px;color:#fff;
                          font-weight:700;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;">
                Assignment Group
              </th>
              <th style="padding:9px 10px;text-align:left;font-size:12px;color:#fff;
                          font-weight:700;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;">
                Owner
              </th>
              <th style="padding:9px 10px;text-align:center;font-size:12px;color:#fff;
                          font-weight:700;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;">
                Days Open
              </th>
              <th style="padding:9px 10px;text-align:left;font-size:12px;color:#fff;
                          font-weight:700;text-transform:uppercase;letter-spacing:0.4px;white-space:nowrap;">
                Opened
              </th>
            </tr>
          </thead>
          <tbody>
            {table_rows}
          </tbody>
        </table>
      </td>
    </tr>

    <!-- footer -->
    <tr>
      <td style="background:#f5f5f5;padding:12px 28px;border-top:1px solid #e0e0e0;">
        <p style="margin:0;font-size:12px;color:#888;text-align:center;">
          &#9200; Generated on {now_str} &nbsp;|&nbsp; Automated alert by CDS ROR tooling
        </p>
      </td>
    </tr>

  </table>
</td></tr>
</table>
</body></html>"""


# ─── Email send ────────────────────────────────────────────────────────────────

def send_alert_email(html_body: str, count: int, quarter_only: bool = True) -> bool:
    try:
        import win32com.client as win32
    except ImportError:
        print("❌ pywin32 not installed. Run: pip install pywin32")
        return False

    recipients = load_recipients()
    if not recipients:
        print("❌ No recipients in config_sharepoint.json → email.recipients")
        return False

    today = datetime.now(IST)
    ww = get_intel_ww()
    quarter_num = (today.month - 1) // 3 + 1
    period = f"Q{quarter_num} {today.year}" if quarter_only else "All Open"
    subject = f"⚠️ CDS ROR — {count} Open Incident{'s' if count != 1 else ''} | {period} | WW{ww:02d}"

    print(f"📧 Sending alert via Outlook…")
    print(f"   To      : {', '.join(recipients)}")
    print(f"   Subject : {subject}")

    try:
        outlook = win32.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.Subject = subject
        mail.BodyFormat = 2
        mail.HTMLBody = html_body
        mail.To = "; ".join(recipients)
        mail.Send()
        print("✅ Alert email sent!")
        return True
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False


# ─── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Alert email for open incidents in the current quarter")
    parser.add_argument("--all", action="store_true", help="Show all open incidents (ignore quarter filter)")
    parser.add_argument("--no-wiki", action="store_true", help="Skip wiki roster fetch")
    args = parser.parse_args()
    quarter_only = not args.all

    print(f"\n{'='*60}")
    print(f"  🚨 CDS ROR — Open Incidents (New / In Progress / On Hold)")
    print(f"{'='*60}")
    q_start = current_quarter_start()
    quarter_num = (datetime.now(IST).month - 1) // 3 + 1
    period_str = f"Q{quarter_num} {datetime.now(IST).year} (from {q_start.strftime('%b %d, %Y')})" if quarter_only else "All time"
    print(f"  States    : New, In Progress, On Hold")
    print(f"  Period    : {period_str}")
    print(f"  Data file : {CSV_FILE}")
    print(f"{'='*60}\n")

    # ── Step 1: Filter incidents ───────────────────────────────────────────
    print(f"🔍 Filtering open incidents (New / In Progress / On Hold)…")
    incidents = get_new_incidents(quarter_only=quarter_only)
    print(f"   Found {len(incidents)} open incident(s)\n")

    # ── Step 2: Fetch on-call roster ──────────────────────────────────────
    roster_html = None
    if not args.no_wiki:
        print("🌐 Fetching on-call roster from Intel Wiki…")
        roster_html = await fetch_roster_html()
        if roster_html:
            oncall_name = parse_oncall_from_html(roster_html)
            if oncall_name:
                print(f"   ✅ Current on-call: {oncall_name}\n")
            else:
                print("   ⚠️  Could not parse on-call name from roster table\n")
        else:
            print("   ⚠️  Wiki fetch failed — skipping on-call info\n")
    else:
        print("⏭️  Skipping wiki fetch (--no-wiki flag)\n")

    # ── Step 3: Build & send email ────────────────────────────────────────
    print("📧 Building alert email…")
    html_body = build_alert_email(incidents, roster_html, quarter_only)
    send_alert_email(html_body, len(incidents), quarter_only)


if __name__ == "__main__":
    asyncio.run(main())
