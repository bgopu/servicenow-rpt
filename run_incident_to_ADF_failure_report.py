"""
Full pipeline runner — executes all steps in sequence:
  1. Download ServiceNow incidents
  2. Correlate incidents with ADF pipeline failures
  3. Generate HTML report
  4. Send report via email
  5. Upload report to SharePoint

Usage:
    python run_pipeline.py                  # all steps, last 14 days ADF lookback
    python run_pipeline.py --days 7         # shorter ADF lookback window
    python run_pipeline.py --skip-email     # skip email step
    python run_pipeline.py --skip-sharepoint  # skip SharePoint upload
    python run_pipeline.py --skip-adf       # skip ADF correlation
"""

import argparse
import asyncio
import sys
import traceback
from pathlib import Path

# Add src/ to path so all module imports work without changes
sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd

# ── Step helpers ──────────────────────────────────────────────────────────────

INCIDENTS_CSV = Path(__file__).parent / "reports" / "Incidents_list.csv"


def step(label: str):
    print(f"\n{'='*70}")
    print(f"  STEP: {label}")
    print(f"{'='*70}")


def ok(msg: str):
    print(f"  ✔  {msg}")


def fail(msg: str):
    print(f"  ✘  {msg}")


# ── 1. Download ServiceNow incidents ─────────────────────────────────────────

async def run_download():
    step("Download ServiceNow incidents")
    from servicenow_downloader import run as snow_run
    success = await snow_run(download_only=True, reset_session=False, upload_to_sharepoint=False)
    if success:
        ok("Incidents downloaded → reports/Incidents_list.csv")
    else:
        fail("Download failed — continuing with existing CSV if present")
    return success


# ── 2. ADF correlation ────────────────────────────────────────────────────────

def run_adf_correlation(days_back: int):
    step(f"Correlate with ADF failures (last {days_back} days)")
    from correlate_incidents_adf import run_correlation
    try:
        run_correlation(days_back)
        ok("ADF correlation complete → reports/adf_errors.json")
        return True
    except Exception as e:
        fail(f"ADF correlation error: {e}")
        traceback.print_exc()
        return False


# ── 3. Generate HTML report ───────────────────────────────────────────────────

def run_report():
    step("Generate HTML report")
    if not INCIDENTS_CSV.exists():
        fail(f"Incidents CSV not found: {INCIDENTS_CSV}")
        return False
    try:
        from report_generator import ReportGenerator

        df = pd.read_csv(INCIDENTS_CSV, low_memory=False)
        df = df.dropna(how="all")
        if "Number" in df.columns:
            df = df.dropna(subset=["Number"]).drop_duplicates(subset=["Number"], keep="first")
        if "Short description" in df.columns:
            df = df.dropna(subset=["Short description"])

        incidents = df.to_dict("records")
        ok(f"Loaded {len(incidents):,} incidents from CSV")

        include_analysis = len(incidents) > 0 and "Short description" in incidents[0]
        gen = ReportGenerator(incidents, include_analysis=include_analysis)
        report_path = gen.to_html()
        ok(f"HTML report → {report_path}")
        return Path(report_path)
    except Exception as e:
        fail(f"Report generation error: {e}")
        traceback.print_exc()
        return False


# ── 4. Send email ─────────────────────────────────────────────────────────────

def run_email(report_path):
    step("Send report via email")
    try:
        from send_email import send_report
        success = send_report(report_path=report_path if report_path else None)
        if success:
            ok("Email sent")
        else:
            fail("Email sending failed")
        return success
    except Exception as e:
        fail(f"Email error: {e}")
        traceback.print_exc()
        return False


# ── 5. SharePoint upload ──────────────────────────────────────────────────────

async def run_sharepoint():
    step("Upload to SharePoint")
    try:
        from sharepoint_uploader import run as sp_run
        success = await sp_run(csv_only=False, report_only=False, reset_session=False)
        if success:
            ok("Uploaded to SharePoint")
        else:
            fail("SharePoint upload failed")
        return success
    except Exception as e:
        fail(f"SharePoint error: {e}")
        traceback.print_exc()
        return False


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Run the full ServiceNow reporting pipeline")
    parser.add_argument("--days",             type=int, default=14,  help="ADF lookback window in days (default: 14)")
    parser.add_argument("--skip-download",    action="store_true",   help="Skip ServiceNow download (use existing CSV)")
    parser.add_argument("--skip-adf",         action="store_true",   help="Skip ADF correlation")
    parser.add_argument("--skip-email",       action="store_true",   help="Skip sending email")
    parser.add_argument("--skip-sharepoint",  action="store_true",   help="Skip SharePoint upload")
    args = parser.parse_args()

    results = {}

    # Step 1
    if not args.skip_download:
        results["download"] = await run_download()
    else:
        print("\n[Skipped] ServiceNow download")

    # Step 2
    if not args.skip_adf:
        results["adf"] = run_adf_correlation(args.days)
    else:
        print("\n[Skipped] ADF correlation")

    # Step 3 — always runs (core output)
    report_path = run_report()
    results["report"] = bool(report_path)

    # Step 4
    if not args.skip_email:
        results["email"] = run_email(report_path if report_path else None)
    else:
        print("\n[Skipped] Email")

    # Step 5
    if not args.skip_sharepoint:
        results["sharepoint"] = await run_sharepoint()
    else:
        print("\n[Skipped] SharePoint upload")

    # Summary
    print(f"\n{'='*70}")
    print("  PIPELINE COMPLETE")
    print(f"{'='*70}")
    for name, passed in results.items():
        status = "✔" if passed else "✘"
        print(f"  {status}  {name}")

    failed = [k for k, v in results.items() if not v]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
