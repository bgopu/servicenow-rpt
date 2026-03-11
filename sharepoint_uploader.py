"""
SharePoint File Uploader (Playwright + Intel SSO)
===================================================
Uploads the incidents CSV and HTML report to SharePoint using browser automation.
Works exactly like the ServiceNow downloader — SSO login once, then headless.

USAGE:
  python sharepoint_uploader.py                    # Upload CSV + latest report
  python sharepoint_uploader.py --reset-session    # Clear saved session (force re-login)
  python sharepoint_uploader.py --csv-only         # Upload only the CSV
  python sharepoint_uploader.py --report-only      # Upload only the HTML report
"""

import asyncio
import argparse
import glob
import json
import os
import sys
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

# ─── CONFIG (reads from config_sharepoint.json) ────────────────────────────

CONFIG_FILE   = Path("config_sharepoint.json")
SESSION_FILE  = Path(".sharepoint_session.json")

with open(CONFIG_FILE) as f:
    _cfg = json.load(f)["sharepoint"]

SP_SITE_URL     = _cfg["site_url"].rstrip("/")                          # https://intel.sharepoint.com/sites/cds_ror
SP_INPUT_FOLDER = _cfg["input_folder"]                                   # .../Servicenow Report Automation
SP_OUTPUT_FOLDER = _cfg["output_folder"]                                 # .../Reports

# Local files to upload
CSV_FILE    = Path("reports/Incidents_list.csv")

# ──────────────────────────────────────────────────────────────────────────────


def latest_report() -> Path | None:
    reports = sorted(
        glob.glob("reports/ServicenowReport_WW*.html"),
        key=os.path.getmtime,
        reverse=True,
    )
    return Path(reports[0]) if reports else None


def print_banner():
    print("\n" + "=" * 60)
    print("  📤 SharePoint Uploader — Intel SSO")
    print("=" * 60)
    print(f"  Site   : {SP_SITE_URL}")
    print(f"  Time   : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60 + "\n")


async def check_logged_in(page) -> bool:
    """Return True if we're on a SharePoint page (not an IdP/login redirect)."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        url = page.url.lower()
        if any(x in url for x in ["login.microsoftonline", "login.live", "idp.", "okta", "ping", "sso/"]):
            return False
        if "sharepoint.com" in url or "intel.sharepoint" in url:
            return True
        return False
    except Exception:
        return False


async def do_sso_login(context, page) -> bool:
    """Open headed browser and wait for user to complete Intel SSO."""
    print("🔐 Intel SSO login required for SharePoint.")
    print("   ➜  Complete the login in the browser window that opens.")
    print("   ➜  The script will continue automatically once you're in.\n")

    await page.goto(SP_SITE_URL, wait_until="domcontentloaded")
    print("⏳ Waiting for SSO login (max 3 minutes)…\n")

    try:
        await page.wait_for_function(
            """() => {
                const url = window.location.href.toLowerCase();
                return (url.includes('sharepoint.com') || url.includes('intel.sharepoint')) &&
                       !url.includes('login.microsoftonline') &&
                       !url.includes('login.live') &&
                       !url.includes('idp.') &&
                       !url.includes('sso/');
            }""",
            timeout=180_000,
        )
        await page.wait_for_timeout(2000)
        await context.storage_state(path=str(SESSION_FILE))
        print("✅ SSO login successful!")
        print(f"💾 Session saved → {SESSION_FILE}\n")
        return True
    except Exception as e:
        print(f"❌ SSO timed out or failed: {e}")
        return False


async def ensure_folder_exists(page, sp_folder: str) -> bool:
    """Create SharePoint folder if it doesn't exist."""
    result = await page.evaluate(
        """async ([siteUrl, folderPath]) => {
            // Check if folder exists
            try {
                const checkResp = await fetch(
                    siteUrl + `/_api/web/GetFolderByServerRelativeUrl('${encodeURIComponent(folderPath)}')`,
                    { headers: { 'Accept': 'application/json;odata=verbose' }, credentials: 'include' }
                );
                if (checkResp.ok) return { success: true, existed: true };
            } catch (e) {}

            // Get digest to create folder
            let digest = '';
            try {
                const digestResp = await fetch(siteUrl + '/_api/contextinfo', {
                    method: 'POST',
                    headers: { 'Accept': 'application/json;odata=verbose', 'Content-Type': 'application/json;odata=verbose' },
                    credentials: 'include'
                });
                const digestJson = await digestResp.json();
                digest = digestJson.d.GetContextWebInformation.FormDigestValue;
            } catch (e) {
                return { success: false, error: 'digest failed: ' + e.toString() };
            }

            // Create folder
            try {
                const createResp = await fetch(siteUrl + '/_api/web/folders', {
                    method: 'POST',
                    headers: {
                        'Accept': 'application/json;odata=verbose',
                        'Content-Type': 'application/json;odata=verbose',
                        'X-RequestDigest': digest,
                    },
                    credentials: 'include',
                    body: JSON.stringify({ '__metadata': { 'type': 'SP.Folder' }, 'ServerRelativeUrl': folderPath })
                });
                return { success: createResp.ok, status: createResp.status };
            } catch (e) {
                return { success: false, error: e.toString() };
            }
        }""",
        [SP_SITE_URL, sp_folder],
    )
    return result.get("success", False)


async def upload_file_via_api(page, local_file: Path, sp_folder: str) -> bool:
    """
    Upload a file to SharePoint using the REST API (/_api/web/...).
    """
    filename = local_file.name
    print(f"   📄 {filename} → {sp_folder}")

    try:
        with open(local_file, "rb") as f:
            file_bytes = f.read()

        import base64
        file_b64 = base64.b64encode(file_bytes).decode("utf-8")

        # Navigate to the site to ensure cookies are active for this origin
        site_page = SP_SITE_URL + "/_layouts/15/start.aspx"
        current_url = page.url
        if "_layouts" not in current_url and "_api" not in current_url:
            await page.goto(SP_SITE_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)

        upload_url = (
            f"{SP_SITE_URL}/_api/web/"
            f"GetFolderByServerRelativeUrl('{sp_folder}')"
            f"/Files/add(url='{filename}',overwrite=true)"
        )

        result = await page.evaluate(
            """async ([uploadUrl, b64data, siteUrl]) => {
                // Decode base64 to binary
                const binaryStr = atob(b64data);
                const bytes = new Uint8Array(binaryStr.length);
                for (let i = 0; i < binaryStr.length; i++) {
                    bytes[i] = binaryStr.charCodeAt(i);
                }
                const blob = new Blob([bytes]);

                // Get request digest
                let digest = '';
                try {
                    const digestResp = await fetch(
                        siteUrl + '/_api/contextinfo',
                        {
                            method: 'POST',
                            headers: {
                                'Accept': 'application/json;odata=verbose',
                                'Content-Type': 'application/json;odata=verbose'
                            },
                            credentials: 'include'
                        }
                    );
                    if (!digestResp.ok) {
                        const txt = await digestResp.text();
                        return { success: false, error: 'contextinfo failed: ' + digestResp.status + ' ' + txt.substring(0, 200) };
                    }
                    const digestJson = await digestResp.json();
                    digest = digestJson.d.GetContextWebInformation.FormDigestValue;
                } catch (e) {
                    return { success: false, error: 'Failed to get form digest: ' + e.toString() };
                }

                // Upload file
                try {
                    const resp = await fetch(uploadUrl, {
                        method: 'POST',
                        headers: {
                            'Accept': 'application/json;odata=verbose',
                            'X-RequestDigest': digest,
                        },
                        credentials: 'include',
                        body: blob,
                    });
                    if (resp.ok) {
                        return { success: true, status: resp.status };
                    } else {
                        const text = await resp.text();
                        return { success: false, status: resp.status, error: text.substring(0, 300) };
                    }
                } catch (e) {
                    return { success: false, error: e.toString() };
                }
            }""",
            [upload_url, file_b64, SP_SITE_URL],
        )

        if result.get("success"):
            size_kb = round(len(file_bytes) / 1024, 1)
            print(f"   ✅ Uploaded ({size_kb} KB)")
            return True
        else:
            print(f"   ❌ Upload failed: {result.get('error', 'unknown error')}")
            return False

    except Exception as e:
        print(f"   ❌ Exception during upload: {e}")
        return False


async def run(csv_only: bool = False, report_only: bool = False, reset_session: bool = False):
    print_banner()

    if reset_session and SESSION_FILE.exists():
        SESSION_FILE.unlink()
        print(f"🗑️  Cleared saved session ({SESSION_FILE})\n")

    # Determine files to upload
    files_to_upload = []  # [(local_path, sp_folder)]

    if not report_only:
        if CSV_FILE.exists():
            files_to_upload.append((CSV_FILE, SP_INPUT_FOLDER))
        else:
            print(f"⚠️  CSV not found: {CSV_FILE}")

    if not csv_only:
        report = latest_report()
        if report:
            files_to_upload.append((report, SP_OUTPUT_FOLDER))
            print(f"📊 Latest report: {report.name}")
        else:
            print("⚠️  No HTML report found in reports/")

    if not files_to_upload:
        print("❌ No files to upload.")
        return False

    print(f"\n📤 Files to upload: {len(files_to_upload)}\n")

    has_session = SESSION_FILE.exists()
    headless    = has_session

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        ctx_opts = {"viewport": {"width": 1280, "height": 900}}
        if has_session:
            ctx_opts["storage_state"] = str(SESSION_FILE)

        context = await browser.new_context(**ctx_opts)
        page    = await context.new_page()

        # ── Verify / refresh session ────────────────────────────────────────
        logged_in = False

        if has_session:
            print("🔄 Verifying saved SharePoint session…")
            await page.goto(SP_SITE_URL, wait_until="domcontentloaded")
            logged_in = await check_logged_in(page)

            if not logged_in:
                print("⚠️  Session expired — re-opening browser for SSO login…\n")
                await browser.close()
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

        # ── Navigate to site root for API calls ────────────────────────────
        current_url = page.url
        if SP_SITE_URL not in current_url:
            await page.goto(SP_SITE_URL, wait_until="domcontentloaded")

        # ── Upload files ────────────────────────────────────────────────────
        success_count = 0
        for local_file, sp_folder in files_to_upload:
            await ensure_folder_exists(page, sp_folder)
            ok = await upload_file_via_api(page, local_file, sp_folder)
            if ok:
                success_count += 1

        await browser.close()

        # ── Summary ─────────────────────────────────────────────────────────
        print(f"\n{'✅' if success_count == len(files_to_upload) else '⚠️ '} "
              f"Uploaded {success_count}/{len(files_to_upload)} files to SharePoint")

        if success_count > 0:
            print(f"\n🔗 SharePoint folder:")
            print(f"   {SP_SITE_URL}/Shared Documents/General/KTBR/Incidents Reduction 2026/Servicenow Report Automation")

        return success_count == len(files_to_upload)


def main():
    parser = argparse.ArgumentParser(
        description="Upload incidents CSV and HTML report to SharePoint via Intel SSO"
    )
    parser.add_argument("--csv-only",      action="store_true", help="Upload only the CSV file")
    parser.add_argument("--report-only",   action="store_true", help="Upload only the HTML report")
    parser.add_argument("--reset-session", action="store_true", help="Clear saved session (force re-login)")
    args = parser.parse_args()

    success = asyncio.run(run(
        csv_only=args.csv_only,
        report_only=args.report_only,
        reset_session=args.reset_session,
    ))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
