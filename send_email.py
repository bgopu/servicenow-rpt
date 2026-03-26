"""Send the latest HTML incident report via Outlook."""
import os
import sys
import json
import glob
import argparse
from pathlib import Path
from datetime import datetime
import pytz

try:
    import win32com.client as win32
except ImportError:
    print("❌ pywin32 not installed. Run: pip install pywin32")
    sys.exit(1)


# ─── CONFIG ───────────────────────────────────────────────────────────────────

CONFIG_FILE = Path("config_sharepoint.json")

# ──────────────────────────────────────────────────────────────────────────────


def get_intel_work_week() -> str:
    """Return current Intel Work Week string e.g. 'WW10'."""
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    year_start = ist.localize(datetime(now.year, 1, 1))
    days = (now - year_start).days + 1  # Jan 1 = day 1
    # WW01: days 1-3  |  WW02: days 4-10  |  WW03: days 11-17 …
    if days < 4:
        ww = 1
    else:
        ww = ((days - 4) // 7) + 2
    return f"WW{ww:02d}"


def load_recipients() -> list[str]:
    """Read recipient list from config_sharepoint.json."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        recipients = cfg.get("email", {}).get("recipients", [])
        if recipients:
            return [r.strip() for r in recipients if r.strip()]
    return []


def find_latest_report() -> Path | None:
    """Return path to the most recently generated HTML report."""
    reports = sorted(
        glob.glob("reports/ServicenowReport_WW*.html"),
        key=os.path.getmtime,
        reverse=True,
    )
    return Path(reports[0]) if reports else None


def build_yoy_html() -> str:
    """Compute live YoY data and return an Outlook-compatible HTML table block."""
    try:
        import pandas as pd

        # ── 2025 baseline ──────────────────────────────────────────────────
        baseline_file = Path("reports/2025_data.csv")
        if not baseline_file.exists():
            return ""
        b_df = pd.read_csv(baseline_file, skiprows=1)
        b_df = b_df[~b_df["Month"].isna()]
        b_df["Year"]    = b_df["Year"].ffill()
        b_df["Quarter"] = b_df["Quarter"].ffill()
        b_df["Count"]   = pd.to_numeric(b_df["Count"], errors="coerce")
        b_df = b_df[b_df["Count"].notna()]

        b_total = int(b_df["Count"].sum())
        b_avg   = round(b_df["Count"].mean(), 1)
        b_q1    = int(b_df[b_df["Quarter"] == "Q1"]["Count"].sum())
        b_q2    = int(b_df[b_df["Quarter"] == "Q2"]["Count"].sum())
        b_q3    = int(b_df[b_df["Quarter"] == "Q3"]["Count"].sum())
        b_q4    = int(b_df[b_df["Quarter"] == "Q4"]["Count"].sum())

        # ── 2025 platform/CDS split (same rule as 2026: ICC L0 + ESD S2P Technical L1) ──
        b_platform_outage = 0
        backup_file = Path("reports/Incidents_list_backup.csv")
        if backup_file.exists():
            try:
                bk = pd.read_csv(backup_file, low_memory=False)
                bk = bk.dropna(how="all").drop_duplicates()
                date_col_bk = next((c for c in bk.columns if "opened" in c.lower()), None)
                if date_col_bk:
                    bk["_opened"] = pd.to_datetime(bk[date_col_bk], format="ISO8601", errors="coerce")
                    bk25 = bk[bk["_opened"].dt.year == 2025]
                    ag_col_bk = next((c for c in bk25.columns if "assignment" in c.lower() and "group" in c.lower()), None)
                    if ag_col_bk:
                        plat25_raw = int(bk25[bk25[ag_col_bk].isin(["ICC L0", "ESD S2P Technical L1"])].shape[0])
                        bk25_total = len(bk25)
                        if bk25_total > 0:
                            b_platform_outage = round(plat25_raw * b_total / bk25_total)
                        else:
                            b_platform_outage = plat25_raw
            except Exception:
                pass
        b_cds_ror_2025 = b_total - b_platform_outage

        # ── 2026 YTD ───────────────────────────────────────────────────────
        csv_file = Path("reports/Incidents_list.csv")
        if not csv_file.exists():
            return ""
        i_df = pd.read_csv(csv_file, low_memory=False)
        i_df = i_df.dropna(how="all").drop_duplicates()

        # Parse date
        date_col = next((c for c in i_df.columns if "opened" in c.lower()), None)
        if date_col:
            i_df["_opened"] = pd.to_datetime(i_df[date_col], format="ISO8601", errors="coerce")
            df_2026 = i_df[i_df["_opened"].dt.year == 2026].copy()
        else:
            df_2026 = i_df.copy()

        total_2026 = len(df_2026)
        # Platform outage: ICC L0 + ESD S2P Technical L1
        ag_col = next((c for c in df_2026.columns if "assignment" in c.lower() and "group" in c.lower()), None)
        platform_outage = 0
        if ag_col:
            platform_outage = len(df_2026[df_2026[ag_col].isin(["ICC L0", "ESD S2P Technical L1"])])
        cds_ror = total_2026 - platform_outage

        # IAO / Non-IAO split excluding platform outage
        # Uses same domain logic as report_generator: strip azmcd/azm prefix, check bare name starts with cp/ip/if
        desc_col = next((c for c in df_2026.columns if "short" in c.lower() or "description" in c.lower()), None)
        iao_excl_platform = 0

        def _is_iao(desc):
            if pd.isna(desc):
                return False
            d = str(desc).lower()
            job_part = d.split('^')[0].strip()
            for pfx in ('azmcd', 'azmxd', 'azm'):
                if job_part.startswith(pfx):
                    job_part = job_part[len(pfx):]
                    break
            is_iao_prefix = any(job_part.startswith(p) for p in ('cp', 'ip', 'if'))
            # IBDS without IAO prefix → not IAO; IBDS with cp/ip/if prefix → IAO
            has_ibds = any(x in d for x in ['ibds', 'ibdsingst', 'cpibdsingst'])
            if has_ibds and not is_iao_prefix:
                return False
            if d.startswith('iao') or ' iao ' in d:
                return True
            return is_iao_prefix
            iao_excl_platform = int((is_iao_series & ~is_platform).sum())
        non_iao_excl_platform = total_2026 - platform_outage - iao_excl_platform

        # Dynamic month range e.g. "Jan - Mar"
        latest_month = df_2026["_opened"].max().strftime("%b") if not df_2026.empty else "Dec"
        month_range = "Jan - " + latest_month

        days_ytd = (datetime.now() - datetime(2026, 1, 1)).days or 1
        projection = round(cds_ror / days_ytd * 365)
        # Compare CDS-owned projection vs CDS-owned 2025 baseline (apples-to-apples)
        vs_pct     = round((projection - b_cds_ror_2025) / b_cds_ror_2025 * 100) if b_cds_ror_2025 > 0 else 0
        trend_color  = "#2e7d32" if vs_pct < 0 else "#c62828"
        trend_label  = f"{abs(vs_pct)}% better ✓" if vs_pct < 0 else f"{vs_pct}% higher"

        def stat_row(label, value, nowrap=False):
            wrap = "white-space:nowrap;" if nowrap else ""
            return f"""
            <tr>
              <td style="font-size:13px;color:#555;padding:7px 0;border-bottom:1px solid #f0f0f0;border-collapse:collapse;">{label}</td>
              <td align="right" style="font-size:13px;font-weight:700;color:#222;padding:7px 0;border-bottom:1px solid #f0f0f0;{wrap}">{value}</td>
            </tr>"""

        def stat_row_stacked(label, value):
            """Label on top line, value on the next line (full-width cell)."""
            return f"""
            <tr>
              <td colspan="2" style="font-size:13px;padding:7px 0;border-bottom:1px solid #f0f0f0;border-collapse:collapse;">
                <span style="color:#555;">{label}</span><br>
                <span style="font-weight:700;color:#222;font-size:13px;">{value}</span>
              </td>
            </tr>"""

        html = f"""
        <!-- YoY Comparison – live data -->
        <p style="margin:0 0 12px 0;font-size:13px;font-weight:700;color:#0068b8;text-transform:uppercase;letter-spacing:0.5px;">
          &#128202;&nbsp; Year-over-Year Comparison (2025 vs 2026)
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px 0;border-collapse:collapse;">
          <tr>

            <!-- 2025 Baseline card -->
            <td width="48%" valign="top" style="background-color:#ffffff;border:1px solid #dde3ea;border-left:4px solid #e53935;border-radius:5px;padding:14px 16px;">
              <p style="margin:0 0 10px 0;font-size:13px;font-weight:700;color:#c62828;">&#128197;&nbsp; 2025 Full Year Baseline</p>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
                {stat_row("Total Incidents", f'<span style="font-size:18px;color:#c62828;">{b_total}</span>')}
                {stat_row("Platform Outage L0/L1 &mdash; est.", b_platform_outage)}
                {stat_row('<span style="font-weight:700;">CDS Owned (excl. platform)</span>', f'<span style="font-size:16px;font-weight:700;color:#c62828;">{b_cds_ror_2025}</span>')}
                {stat_row("Monthly Average", b_avg)}
                {stat_row("Q1 / Q2", f"{b_q1} &nbsp;/&nbsp; {b_q2}")}
                {stat_row("Q3 / Q4", f"{b_q3} &nbsp;/&nbsp; {b_q4}")}
              </table>
            </td>

            <td width="4%">&nbsp;</td>

            <!-- 2026 YTD card -->
            <td width="48%" valign="top" style="background-color:#ffffff;border:1px solid #dde3ea;border-left:4px solid #43a047;border-radius:5px;padding:14px 16px;">
              <p style="margin:0 0 10px 0;font-size:13px;font-weight:700;color:#2e7d32;">&#10024;&nbsp; 2026 Year-to-Date ({month_range})</p>
              <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
                {stat_row("Total Incidents", f'<span style="font-size:18px;color:#2e7d32;">{total_2026}</span>')}
                {stat_row("Platform Outage (L0, L1) &mdash; IAO + Non IAO", platform_outage)}
                {stat_row("Non IAO Incidents (Excl. Platform Outage)", non_iao_excl_platform)}
                {stat_row("IAO Incidents (Excl. Platform Outage)", iao_excl_platform)}
                {stat_row('<span style="font-weight:700;">Projected 2026 (CDS owned)</span>', f'<span style="white-space:nowrap;">{projection}&nbsp;<span style="font-size:11px;color:{trend_color};font-weight:700;">({trend_label})</span></span>', nowrap=True)}
              </table>
            </td>

          </tr>
        </table>
        <p style="margin:8px 0 0 0;font-size:11px;color:#888;font-style:italic;">
          &#9888;&nbsp; Platform incidents (ICC L0 / ESD S2P Technical L1) are excluded from both the 2026
          projection and the 2025 baseline to ensure a fair, like-for-like comparison. The 2025 platform
          figure is estimated from backup data and proportionally scaled to align with the verified 2025 total.
        </p>"""
        return html

    except Exception as e:
        print(f"⚠️  YoY section skipped: {e}")
        return ""


def send_report(report_path: Path | None = None, extra_recipients: list[str] | None = None) -> bool:
    """
    Send the HTML report via Outlook.

    Args:
        report_path:       Path to the HTML file. Uses latest report if None.
        extra_recipients:  Additional To: addresses beyond config.json list.

    Returns:
        True on success, False on failure.
    """
    # ── Find report ────────────────────────────────────────────────────────
    if report_path is None:
        report_path = find_latest_report()

    if report_path is None or not Path(report_path).exists():
        print("❌ No report file found. Generate a report first.")
        return False

    report_path = Path(report_path)

    # ── Recipients ─────────────────────────────────────────────────────────
    recipients = load_recipients()
    if extra_recipients:
        recipients += [r.strip() for r in extra_recipients if r.strip()]
    if not recipients:
        print("❌ No recipients configured. Add email.recipients in config_sharepoint.json")
        return False

    # ── Compute live YoY section ────────────────────────────────────────────
    yoy_html = build_yoy_html()

    # ── Subject & metadata ─────────────────────────────────────────────────
    ww      = get_intel_work_week()
    ist     = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    subject = f"CDS ROR - ServiceNow Incident Report - {ww}"
    generated_on = now_ist.strftime("%B %d, %Y at %I:%M %p")

    # ── ServiceNow deep-link ───────────────────────────────────────────────
    sn_url = (
        "https://intel.service-now.com/incident_list.do?"
        "sysparm_query=business_service.nameINCDS ROR,Core Data Solutions"
        "&sysparm_view=&sysparm_userpref_module=0"
    )

    # ── Email body HTML (table-based, Outlook-compatible) ─────────────────
    html_body = f"""<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!--[if mso]><xml><o:OfficeDocumentSettings><o:AllowPNG/><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#f0f0f0;font-family:Segoe UI,Arial,sans-serif;">

<!-- Outer wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f0f0f0;">
<tr><td align="center" style="padding:24px 16px;">

  <!-- Card -->
  <table width="580" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border-radius:6px;overflow:hidden;border:1px solid #dde3ea;">

    <!-- ── HEADER ── -->
    <tr>
      <td bgcolor="#0068b8" style="padding:26px 32px;background-color:#0068b8;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <td>
              <p style="margin:0 0 4px 0;font-size:20px;font-weight:700;color:#ffffff;line-height:1.3;">
                &#128202; CDS ROR &#8212; ServiceNow Incident Report
              </p>
              <p style="margin:0;font-size:13px;color:#cce4f6;line-height:1.4;">
                Automated incident analytics &nbsp;|&nbsp; CDS ROR
              </p>
            </td>
            <td align="right" valign="top">
              <table cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="background-color:#005a9e;border-radius:20px;padding:5px 16px;">
                    <span style="font-size:13px;font-weight:700;color:#ffffff;">&#128197; {ww}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- ── BODY ── -->
    <tr>
      <td style="padding:28px 32px;">

        <!-- Greeting -->
        <p style="margin:0 0 8px 0;font-size:14px;color:#222222;">Hi Team,</p>
        <p style="margin:0 0 22px 0;font-size:14px;color:#444444;line-height:1.6;">
          Please find attached the ServiceNow Incident Report for <strong>CDS ROR</strong> with detailed insights.
        </p>

        <!-- Divider -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #e8ecf0;padding-bottom:20px;"></td></tr></table>

        {yoy_html}

        <!-- Divider -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #e8ecf0;padding-bottom:20px;"></td></tr></table>

        <!-- How to View -->
        <p style="margin:0 0 8px 0;font-size:13px;font-weight:700;color:#0068b8;text-transform:uppercase;letter-spacing:0.5px;">
          &#128206;&nbsp; How to View the Report
        </p>
        <div style="margin:0 0 20px 0;">
          <p style="margin:0;padding:3px 0;font-size:14px;color:#444;">
            <span style="color:#0068b8;font-weight:700;">1.</span>&nbsp; Download / open the attached <strong>HTML file</strong> in your web browser (Chrome, Edge, or Firefox)
          </p>
          <p style="margin:0;padding:3px 0;font-size:14px;color:#444;">
            <span style="color:#0068b8;font-weight:700;">2.</span>&nbsp; Explore interactive charts, filters, and detailed analytics
          </p>
        </div>

        <!-- Divider -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #e8ecf0;padding-bottom:20px;"></td></tr></table>

        <!-- Report Features -->
        <p style="margin:0 0 10px 0;font-size:13px;font-weight:700;color:#0068b8;text-transform:uppercase;letter-spacing:0.5px;">
          &#10024;&nbsp; Report Features
        </p>
        <div style="margin:0 0 20px 0;">
          <p style="margin:0;padding:3px 0;font-size:14px;color:#444;">&#128202;&nbsp; Interactive domain distribution chart</p>
          <p style="margin:0;padding:3px 0;font-size:14px;color:#444;">&#128200;&nbsp; Weekly incident trend analysis with WW labeling</p>
          <p style="margin:0;padding:3px 0;font-size:14px;color:#444;">&#128260;&nbsp; Top recurring jobs identification</p>
          <p style="margin:0;padding:3px 0;font-size:14px;color:#444;">&#128269;&nbsp; Advanced filtering by domains, states, priorities, and jobs</p>
          <p style="margin:0;padding:3px 0;font-size:14px;color:#444;">&#11088;&nbsp; Domain performance recognition and benchmarking</p>
        </div>

        <!-- Divider -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #e8ecf0;padding-bottom:20px;"></td></tr></table>

        <!-- ServiceNow Link — warm yellow highlight box -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px 0;">
          <tr>
            <td style="background-color:#fff8e1;border:1px solid #f5c842;border-radius:5px;padding:14px 18px;">
              <p style="margin:0;font-size:14px;color:#444444;line-height:1.7;">
                &#128279;&nbsp; <strong>View Incidents details in ServiceNow:</strong>&nbsp;
                <a href="{sn_url}" style="color:#0068b8;text-decoration:underline;font-weight:600;">click here</a>
              </p>
            </td>
          </tr>
        </table>

        <!-- Divider -->
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td style="border-top:1px solid #e8ecf0;padding-bottom:20px;"></td></tr></table>

      </td>
    </tr>

    <!-- ── FOOTER: Generated on ── -->
    <tr>
      <td style="background-color:#f0f2f5;padding:14px 32px;border-top:1px solid #e0e4ea;">
        <p style="margin:0;font-size:12px;color:#666666;text-align:center;line-height:1.6;">
          &#9200;&nbsp; <strong>Generated on:</strong> {generated_on}
        </p>
      </td>
    </tr>

  </table>
  <!-- /Card -->

  <!-- ── NOTE: below card ── -->
  <table width="580" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;">
    <tr>
      <td style="padding:12px 16px;">
        <p style="margin:0;font-size:12px;color:#888888;text-align:center;line-height:1.6;">
          &#128204;&nbsp; <strong>Note:</strong> This is an automated report. The HTML file contains interactive features that work best when opened in a modern web browser.
        </p>
      </td>
    </tr>
  </table>

</td></tr>
</table>
<!-- /Outer wrapper -->

</body>
</html>"""

    # ── Send via Outlook ───────────────────────────────────────────────────
    print(f"📧 Sending report via Outlook…")
    print(f"   To      : {', '.join(recipients)}")
    print(f"   Subject : {subject}")
    print(f"   Report  : {report_path}")

    try:
        outlook = win32.Dispatch("Outlook.Application")
        mail    = outlook.CreateItem(0)  # olMailItem

        mail.Subject    = subject
        mail.BodyFormat = 2             # olFormatHTML
        mail.HTMLBody   = html_body
        mail.To         = "; ".join(recipients)

        # Attach the HTML report file
        mail.Attachments.Add(str(report_path.resolve()))

        mail.Send()
        print("✅ Email sent successfully!")
        return True

    except Exception as e:
        print(f"❌ Failed to send email: {e}")
        return False


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Send latest incident report via Outlook")
    parser.add_argument(
        "--report",
        default=None,
        help="Path to a specific HTML report file (default: latest in reports/)",
    )
    parser.add_argument(
        "--to",
        nargs="+",
        default=None,
        metavar="EMAIL",
        help="Additional recipients (space-separated)",
    )
    args = parser.parse_args()

    report = Path(args.report) if args.report else None
    success = send_report(report_path=report, extra_recipients=args.to)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
