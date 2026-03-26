"""
ServiceNow Automated CSV Downloader
====================================
Uses Playwright to auto-download CDS ROR incident data from ServiceNow.

HOW IT WORKS:
  - First run  : Opens a visible browser window → you complete Intel SSO login once
  - Next runs  : Uses saved session cookies (headless, no login needed)
  - If session expires → automatically re-opens browser for fresh SSO

USAGE:
  python servicenow_downloader.py             # Download + generate report
  python servicenow_downloader.py --download-only   # Just download CSV
  python servicenow_downloader.py --reset-session   # Clear saved session

"""
import asyncio
import os
import shutil
import subprocess
import sys
import argparse
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

# ─── CONFIG ───────────────────────────────────────────────────────────────────

SERVICENOW_BASE_URL = "https://intel.service-now.com"

# Business service filter — CDS ROR and Core Data Solutions
INCIDENT_QUERY = "business_service.nameINCDS ROR,Core Data Solutions"

# Columns to export — must match reports/Incidents_list.csv headers exactly
EXPORT_FIELDS = (
    "number,caller_id,short_description,business_service,"
    "priority,state,assignment_group,assigned_to,"
    "opened_at,u_breach_reason,u_breach_comments,calendar_stc,sys_tags"
)

_ROOT = Path(__file__).parent.parent

# Saved session file (stores cookies so SSO only happens once)
SESSION_FILE = _ROOT / ".servicenow_session.json"

# Output paths
OUTPUT_CSV  = _ROOT / "reports" / "Incidents_list.csv"
BACKUP_CSV  = _ROOT / "reports" / "Incidents_list_backup.csv"

# ──────────────────────────────────────────────────────────────────────────────


def print_banner(mode: str):
    print("\n" + "=" * 60)
    print("  🔄 ServiceNow Incident Report — Auto Downloader")
    print("=" * 60)
    print(f"  Mode : {mode}")
    print(f"  Time : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60 + "\n")


async def check_logged_in(page) -> bool:
    """Return True if the current page is ServiceNow (not SSO/login redirect)."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        url = page.url.lower()
        if any(x in url for x in ["idp.", "/login", "/sso", "microsoftonline", "okta", "ping"]):
            return False
        if "intel.service-now.com" in url:
            return True
        return False
    except Exception:
        return False


async def do_sso_login(context, page) -> bool:
    """
    Open a headed browser, wait for the user to complete Intel SSO,
    then save the session state for future headless runs.
    """
    print("🔐 Intel SSO login required.")
    print("   ➜  Complete the login in the browser window that opens.")
    print("   ➜  The script will continue automatically once you're in.\n")

    await page.goto(SERVICENOW_BASE_URL, wait_until="domcontentloaded")
    print("⏳ Waiting for SSO login (max 3 minutes)…\n")

    try:
        # Wait until URL is back on ServiceNow proper (not an IdP)
        await page.wait_for_function(
            """() => {
                const url = window.location.href.toLowerCase();
                return url.includes('intel.service-now.com') &&
                       !url.includes('idp.') &&
                       !url.includes('/login') &&
                       !url.includes('microsoftonline') &&
                       !url.includes('okta') &&
                       !url.includes('ping');
            }""",
            timeout=180_000,
        )
        # Extra wait to ensure all session cookies are written
        await page.wait_for_timeout(2000)

        # Save cookies/session for next run
        await context.storage_state(path=str(SESSION_FILE))
        print("✅ SSO login successful!")
        print(f"💾 Session saved → {SESSION_FILE}  (next run will be headless)\n")
        return True

    except Exception as e:
        print(f"❌ SSO timed out or failed: {e}")
        return False


async def download_csv_direct(page) -> bool:
    """
    ServiceNow direct CSV export via URL:
    /incident_list.do?sysparm_query=...&sysparm_fields=...&CSV
    """
    encoded_query  = INCIDENT_QUERY.replace(" ", "%20")
    encoded_fields = EXPORT_FIELDS.replace(",", "%2C")

    csv_url = (
        f"{SERVICENOW_BASE_URL}/incident_list.do"
        f"?sysparm_query={encoded_query}"
        f"&sysparm_fields={encoded_fields}"
        f"&CSV"
    )

    print(f"📥 Downloading CSV from ServiceNow…")
    print(f"   Filter: {INCIDENT_QUERY}\n")

    try:
        # Use 'commit' (not 'domcontentloaded') because a CSV URL triggers a
        # download immediately — the page never fully loads.
        async with page.expect_download(timeout=90_000) as dl_info:
            try:
                await page.goto(csv_url, wait_until="commit", timeout=30_000)
            except Exception:
                pass  # 'Download is starting' error is expected and safe to ignore

        download = await dl_info.value

        if await download.failure():
            print(f"❌ Download reported failure: {await download.failure()}")
            return False

        # Backup existing file before overwriting
        if OUTPUT_CSV.exists():
            shutil.copy2(OUTPUT_CSV, BACKUP_CSV)
            print(f"💾 Backed up previous CSV → {BACKUP_CSV}")

        await download.save_as(str(OUTPUT_CSV))

        # Validate row count
        with open(OUTPUT_CSV, encoding="utf-8", errors="replace") as f:
            rows = f.readlines()
        record_count = max(0, len(rows) - 1)

        print(f"✅ Downloaded {record_count:,} incident records")
        print(f"   Saved → {OUTPUT_CSV}\n")
        return record_count > 0

    except Exception as e:
        print(f"⚠️  Direct CSV download failed: {e}")
        return False


async def download_csv_via_ui(page) -> bool:
    """
    Fallback: Navigate to the classic UI list view and use the
    ServiceNow List > Export > CSV option.
    """
    print("🔄 Trying UI-based CSV export as fallback…\n")
    try:
        # Direct export URL — append &CSV to force download via classic UI
        export_url = (
            f"{SERVICENOW_BASE_URL}/incident_list.do"
            f"?sysparm_query={INCIDENT_QUERY.replace(' ', '+')}"
            f"&sysparm_fields={EXPORT_FIELDS}"
            f"&sysparm_view=&CSV"
        )
        async with page.expect_download(timeout=90_000) as dl_info:
            try:
                await page.goto(export_url, wait_until="commit", timeout=30_000)
            except Exception:
                pass  # Download-start error is expected

        download = await dl_info.value
        await download.save_as(str(OUTPUT_CSV))

        with open(OUTPUT_CSV, encoding="utf-8", errors="replace") as f:
            count = max(0, len(f.readlines()) - 1)

        print(f"✅ UI export: {count:,} records downloaded → {OUTPUT_CSV}\n")
        return count > 0

    except Exception as e:
        print(f"❌ UI export also failed: {e}")
        return False


async def run(download_only: bool = False, reset_session: bool = False, upload_to_sharepoint: bool = False):
    # Reset session if requested
    if reset_session and SESSION_FILE.exists():
        SESSION_FILE.unlink()
        print(f"🗑️  Cleared saved session ({SESSION_FILE})\n")

    # Ensure output folder exists
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    has_session = SESSION_FILE.exists()
    headless    = has_session  # headless only when we have a saved session

    print_banner("Headless (saved session)" if headless else "Headed (SSO login required)")

    async with async_playwright() as pw:

        # ── Launch browser ──────────────────────────────────────────────────
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        ctx_opts = {"viewport": {"width": 1280, "height": 900}}
        if has_session:
            ctx_opts["storage_state"] = str(SESSION_FILE)

        context = await browser.new_context(**ctx_opts)
        page    = await context.new_page()

        # ── Check / refresh session ─────────────────────────────────────────
        logged_in = False

        if has_session:
            print("🔄 Verifying saved session…")
            test_url = f"{SERVICENOW_BASE_URL}/incident_list.do?sysparm_query=number%3DINC0000001"
            await page.goto(test_url, wait_until="domcontentloaded")
            logged_in = await check_logged_in(page)

            if not logged_in:
                print("⚠️  Session expired — re-opening browser for SSO login…\n")
                await browser.close()
                # Re-launch headed for fresh SSO
                browser = await pw.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(viewport={"width": 1280, "height": 900})
                page    = await context.new_page()

        if not logged_in:
            logged_in = await do_sso_login(context, page)

        if not logged_in:
            print("❌ Authentication failed. Exiting.")
            await browser.close()
            return False

        # ── Download CSV ────────────────────────────────────────────────────
        success = await download_csv_direct(page)
        if not success:
            success = await download_csv_via_ui(page)

        await browser.close()

        # ── Generate report ───────────────────────────────────────────────────────────────
        if success and not download_only:
            print("📊 Generating HTML report…")
            result = subprocess.run(
                [sys.executable, "main.py", "--input", str(OUTPUT_CSV)],
                capture_output=False,
            )
            if result.returncode == 0:
                import glob
                reports = sorted(glob.glob("reports/ServicenowReport_WW*.html"), key=os.path.getmtime, reverse=True)
                if reports:
                    latest = reports[0]
                    print(f"\n🌐 Opening report: {latest}")
                    os.startfile(latest)
                if upload_to_sharepoint:
                    print("\n📤 Uploading to SharePoint…")
                    subprocess.run([sys.executable, "sharepoint_uploader.py"], capture_output=False)
                # Always send email with the report
                print("\n📧 Sending report via email…")
                subprocess.run([sys.executable, "send_email.py"], capture_output=False)
            else:
                print(f"⚠️  Report generation returned exit code {result.returncode}")

        elif not success:
            print("❌ CSV download failed — report not generated.")
            print("   ➜  Check your VPN connection and ServiceNow access.")
            print(f"   ➜  Or export manually and save to: {OUTPUT_CSV}")

        return success


def main():
    parser = argparse.ArgumentParser(
        description="Auto-download ServiceNow incidents CSV and generate report"
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Only download the CSV, do not generate the report",
    )
    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="Clear the saved SSO session (forces fresh login next run)",
    )
    parser.add_argument(
        "--upload-to-sharepoint",
        action="store_true",
        help="After generating the report, upload CSV + report to SharePoint",
    )
    args = parser.parse_args()

    success = asyncio.run(run(
        download_only=args.download_only,
        reset_session=args.reset_session,
        upload_to_sharepoint=args.upload_to_sharepoint,
    ))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
