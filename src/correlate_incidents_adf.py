"""
ServiceNow ↔ ADF Correlation

Maps ServiceNow JOBFAILURE incidents to their root ADF pipeline run + error.
Job → pipeline mapping is loaded from Autosys_Job_details_P01.xlsx (SummaryProd sheet).

Scope: ALL domains (Customer, Supplier, Finance, Item, Reference, Worker).

Usage:
    python correlate_incidents_adf.py                                       # last 7 days
    python correlate_incidents_adf.py 14                                    # last 14 days
    python correlate_incidents_adf.py 14 C:\\path\\to\\Incidents_list.csv   # custom CSV
"""

import json
import logging
import os
import sys

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Force UTF-8 output so special chars don't break on Windows terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

logging.getLogger("azure.identity").setLevel(logging.ERROR)
logging.getLogger("azure.core").setLevel(logging.ERROR)

from adf_fetcher import get_credential, get_failed_runs, get_activity_errors, _extract_root_cause

# Project root is one level above src/
_ROOT = Path(__file__).parent.parent

# All domain ADF config files
DOMAIN_CONFIGS = [
    _ROOT / "config" / "config_customer_adf.json",
    _ROOT / "config" / "config_supplier_adf.json",
    _ROOT / "config" / "config_finance_adf.json",
    _ROOT / "config" / "config_item_adf.json",
    _ROOT / "config" / "config_reference_adf.json",
    _ROOT / "config" / "config_worker_adf.json",
]
_default_incidents_csv = _ROOT / "reports" / "Incidents_list.csv"
INCIDENTS_CSV = Path(sys.argv[2]) if len(sys.argv) > 2 else _default_incidents_csv

MAPPING_XLSX = _ROOT / "Autosys_Job_details_P01.xlsx"
SUBJECT_AREA_FILTER = ""  # All domains


# ── Load job → pipeline + RG mapping from Excel ──────────────────────────────
def load_job_pipeline_map(
    xlsx_path: Path = MAPPING_XLSX,
    subject_area: str = SUBJECT_AREA_FILTER,
) -> dict[str, dict]:
    """
    Read Autosys CMD → {pipeline, rg} from the Excel sheet, filtered to a subject area.
    Returns a dict keyed by lowercase job name for case-insensitive exact lookup.
    Each value: {"pipeline": str, "rg": str}
    """
    df = pd.read_excel(xlsx_path, sheet_name="SummaryProd")
    if subject_area:
        df = df[df["Subject Area"].str.contains(subject_area, na=False, case=False)]
    mapping = (
        df[["Autosys CMD", "Pipeline Name", "RG"]]
        .dropna(subset=["Autosys CMD", "Pipeline Name", "RG"])
        .drop_duplicates(subset=["Autosys CMD"])
    )
    result = {
        str(row["Autosys CMD"]).strip().lower(): {
            "pipeline": str(row["Pipeline Name"]).strip(),
            "rg":       str(row["RG"]).strip(),
        }
        for _, row in mapping.iterrows()
    }
    label = subject_area if subject_area else "all domains"
    print(f"[Mapping] Loaded {len(result)} job→pipeline entries ({label}).")
    return result


# Built once at import time
JOB_TO_PIPELINE: dict[str, dict] = load_job_pipeline_map()

def extract_job_name(short_description: str) -> str:
    """Pull the Autosys job name from the incident short description."""
    # Format: '<jobname>^P01 JOBFAILURE <server>'
    if isinstance(short_description, str) and "JOBFAILURE" in short_description:
        return short_description.split("^")[0].strip().lower()
    return ""


# ── RG overrides: fix any incorrect RG values from the Excel ─────────────────
# Maps job name → correct resource group when the Excel has a wrong RG entry
RG_OVERRIDES: dict[str, str] = {
    # azmcdcpcus101 and azmcdcusingssidecar belong to cp-rg-mdcus-prod-sj per Excel
    # (no overrides needed — Excel is correct; access needed on cp-rg-mdcus-prod-sj)
}


def job_to_pipeline(job_name: str) -> dict | None:
    """
    Map an Autosys job name to {pipeline, rg} using the Excel mapping.
    Returns None if the job is outside the current domain scope (POC: Customer only).
    """
    entry = JOB_TO_PIPELINE.get(job_name.lower())
    if entry and job_name.lower() in RG_OVERRIDES:
        entry = {**entry, "rg": RG_OVERRIDES[job_name.lower()]}
    return entry


def find_best_adf_match(adf_runs: list, pipeline_name: str, incident_time) -> dict | None:
    """
    Find the ADF run for a given pipeline that is closest in time to the incident.
    Returns None if no run found within 4 hours.
    """
    if incident_time.tzinfo is None:
        incident_time = incident_time.replace(tzinfo=timezone.utc)

    candidates = [r for r in adf_runs if r.pipeline_name == pipeline_name]
    if not candidates:
        return None

    best = None
    best_delta = timedelta(hours=12)  # max window — ADF may run hours after Autosys job

    for run in candidates:
        run_start = run.run_start
        if run_start is None:
            continue
        if run_start.tzinfo is None:
            run_start = run_start.replace(tzinfo=timezone.utc)

        # Incident opens after or during the ADF run
        delta = abs(incident_time - run_start)
        if delta < best_delta:
            best_delta = delta
            best = run

    return best


def run_correlation(days_back: int = 7):
    # Merge all ADF instances from all domain config files
    adf_instances: list[dict] = []
    for cfg_path in DOMAIN_CONFIGS:
        if not cfg_path.exists():
            print(f"  [Config] {cfg_path.name} not found — skipping")
            continue
        try:
            cfg_data = json.loads(cfg_path.read_text())
            instances = cfg_data.get("adf_instances", [])
            # Add env label if missing
            for inst in instances:
                if "env" not in inst:
                    inst["env"] = inst.get("resource_group", "unknown")
            adf_instances.extend(instances)
        except Exception as e:
            print(f"  [Config] Failed to load {cfg_path.name}: {e}")
    print(f"[Config] Loaded {len(adf_instances)} ADF instance(s) across all domains.")

    # ── Load ServiceNow incidents ──────────────────────────────────────────────
    df = pd.read_csv(INCIDENTS_CSV)
    df["opened_at"] = pd.to_datetime(df["opened_at"], format="ISO8601", errors="coerce")

    cutoff = datetime.now() - timedelta(days=days_back)  # tz-naive to match CSV
    job_fail = df[
        df["short_description"].str.contains("JOBFAILURE", na=False)
        & (df["opened_at"] >= cutoff)
    ].sort_values("opened_at", ascending=False)

    if job_fail.empty:
        print(f"No JOBFAILURE incidents in the last {days_back} days.")
        return

    print(f"Found {len(job_fail)} JOBFAILURE incident(s) in the last {days_back} days.\n")

    # ── Determine which ADF instances are actually needed ─────────────────────
    # Collect exact RGs from incident→job→pipeline mapping
    needed_rgs: set[str] = set()
    for _, inc in job_fail.iterrows():
        jn = extract_job_name(inc["short_description"])
        info = job_to_pipeline(jn)
        if info:
            needed_rgs.add(info["rg"].lower())

    # Match only the exact ADF instance for each needed RG
    target_instances = [
        inst for inst in adf_instances
        if inst["resource_group"].lower() in needed_rgs
    ]

    if not target_instances:
        print("⚠ No ADF instances matched the incident RGs — falling back to all instances.")
        target_instances = adf_instances

    rgs_label = ", ".join(sorted(needed_rgs)) or "all"
    print(f"RGs in scope      : {rgs_label}")
    print(f"ADF instances     : {len(target_instances)} of {len(adf_instances)} (exact match only)")
    print(f"Fetching ADF failed runs ...")
    all_adf_runs: list = []
    # Map run_id → (client, cfg) for later activity drill-down
    run_client_map: dict[str, tuple] = {}
    for inst in target_instances:
        cfg = {
            "subscription_id": inst["subscription_id"],
            "resource_group":  inst["resource_group"],
            "adf_name":        inst["adf_name"],
        }
        label = f"{inst['adf_name']} ({inst['env']})"
        try:
            runs, c = get_failed_runs(cfg, days_back + 1)
            for r in runs:
                run_client_map[r.run_id] = (c, cfg)
            all_adf_runs.extend(runs)
            print(f"  {label}: {len(runs)} failed run(s)")
        except Exception as e:
            print(f"  {label}: ⚠ Could not fetch runs — {e}")
    print(f"Total: {len(all_adf_runs)} failed ADF run(s).\n")

    # Cache activity errors per run_id to avoid duplicate API calls
    error_cache: dict[str, list] = {}

    print("=" * 80)
    print(f"{'INCIDENT':<13}  {'JOB NAME':<30}  {'OPENED (UTC)':<20}")
    print(f"{'ADF PIPELINE':<44}  {'RUN START (UTC)':<22}  STATUS")
    print("=" * 80)

    # Group by ADF run so we only print the error details once per unique match
    seen_run_ids: set[str] = set()
    csv_rows: list[dict] = []

    def make_row(inc, job_name: str) -> dict:
        """Base row with all ServiceNow fields."""
        return {
            "incident":          inc["number"],
            "caller_id":         inc.get("caller_id", ""),
            "short_description": inc.get("short_description", ""),
            "business_service":  inc.get("business_service", ""),
            "priority":          inc.get("priority", ""),
            "state":             inc.get("state", ""),
            "assignment_group":  inc.get("assignment_group", ""),
            "assigned_to":       inc.get("assigned_to", ""),
            "opened_at":         inc["opened_at"].strftime("%Y-%m-%d %H:%M") if hasattr(inc["opened_at"], "strftime") else str(inc["opened_at"]),
            # "u_breach_reason":   inc.get("u_breach_reason", ""),
            "job_name":          job_name,
            "pipeline":          "",
            "failed_activities":  "",
            "failed_step_input":  "",
            "failed_step_output": "",
            "root_cause":         "",
            "rg":                 "",
            "adf":               "",
            "adf_run_id":        "",
            "run_start":         "",
            "run_end":           "",
            
            "status":            "",
        }

    skipped = 0
    for _, inc in job_fail.iterrows():
        job_name = extract_job_name(inc["short_description"])
        job_info = job_to_pipeline(job_name)

        if job_info is None:
            skipped += 1
            row = make_row(inc, job_name)
            row["status"] = "Job not in mapping"
            csv_rows.append(row)
            continue  # no job→pipeline entry found in Autosys_Job_details_P01.xlsx

        pipeline = job_info["pipeline"]
        job_rg   = job_info["rg"]
        inc_time = inc["opened_at"]
        if inc_time.tzinfo is None:
            inc_time = inc_time.replace(tzinfo=timezone.utc)

        adf_run = find_best_adf_match(all_adf_runs, pipeline, inc_time)
        inst_match = next((i for i in adf_instances if i["resource_group"] == job_rg), None)

        row = make_row(inc, job_name)
        row["pipeline"] = pipeline
        row["rg"]       = job_rg
        row["adf"]      = inst_match["adf_name"] if inst_match else "(unknown)"

        print(f"\n{'-'*80}")
        print(f"  INC       : {inc['number']}")
        print(f"  Job Name  : {job_name}")
        print(f"  Opened    : {inc_time.strftime('%Y-%m-%d %H:%M')} UTC")
        print(f"  Pipeline  : {pipeline}")
        print(f"  RG        : {job_rg}")
        print(f"  ADF       : {row['adf']}")

        if adf_run is None:
            print(f"  ADF Match : ⚠ No matching ADF run found within 12 hours")
            row["status"] = "No ADF run matched"
            csv_rows.append(row)
            continue

        run_start = adf_run.run_start
        if run_start and run_start.tzinfo is None:
            run_start = run_start.replace(tzinfo=timezone.utc)

        row["adf_run_id"] = adf_run.run_id
        row["run_start"]  = run_start.strftime("%Y-%m-%d %H:%M") if run_start else ""
        row["run_end"]    = adf_run.run_end.strftime("%Y-%m-%d %H:%M") if adf_run.run_end else ""

        print(f"  ADF Run   : {adf_run.run_id}")
        print(f"  Run Start : {row['run_start']} UTC")
        print(f"  Run End   : {row['run_end']} UTC")

        # Only fetch and print errors once per unique ADF run
        if adf_run.run_id not in seen_run_ids:
            seen_run_ids.add(adf_run.run_id)
            if adf_run.run_id not in error_cache:
                run_cfg_client, run_cfg = run_client_map.get(adf_run.run_id, (None, None))
                if run_cfg_client and run_cfg:
                    error_cache[adf_run.run_id] = get_activity_errors(run_cfg_client, run_cfg, adf_run.run_id)
                else:
                    error_cache[adf_run.run_id] = []
            errors = error_cache[adf_run.run_id]

            root = ""
            if adf_run.message:
                root = _extract_root_cause(str(adf_run.message))
                print(f"  Root Cause: {root}")

            if errors:
                for e in errors:
                    print(f"  Failed At : {e['activity']} [{e['type']}]  ErrCode={e['error_code']}")
                    err_row = dict(row)
                    err_row["failed_activities"]  = f"{e['activity']} [{e['type']}] ErrCode={e['error_code']}"
                    err_row["failed_step_input"]  = e.get("input", "")
                    err_row["failed_step_output"] = e.get("output", "")
                    err_row["root_cause"]         = e.get("message", "") or root
                    err_row["status"]             = "Matched"
                    csv_rows.append(err_row)
            else:
                row["root_cause"] = root
                row["status"]     = "Matched (no activity errors)"
                csv_rows.append(row)
        else:
            print(f"  (Same ADF run as above -- error details already shown)")
            prior = [r for r in csv_rows if r["adf_run_id"] == adf_run.run_id]
            for pr in prior:
                dup = dict(pr)
                dup["incident"]          = row["incident"]
                dup["short_description"] = row["short_description"]
                dup["opened_at"]         = row["opened_at"]
                dup["status"]            = "Matched (same run)"
                csv_rows.append(dup)
            if not prior:
                row["status"] = "Matched (same run)"
                csv_rows.append(row)

    # ── Write Excel output ──────────────────────────────────────────────────
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    EXCEL_HEADERS = [
        ("Incident",           "incident",          14),
        ("Caller",             "caller_id",          20),
        ("Short Description",  "short_description",  45),
        ("Business Service",   "business_service",   25),
        ("Priority",           "priority",           10),
        ("State",              "state",              12),
        ("Assignment Group",   "assignment_group",   28),
        ("Assigned To",        "assigned_to",        22),
        ("Opened (UTC)",       "opened_at",          18),
        ("Breach Reason",      "u_breach_reason",    22),
        ("Job Name",           "job_name",           28),
        ("Pipeline",           "pipeline",           45),
        ("RG",                 "rg",                 28),
        ("ADF",                "adf",                28),
        ("ADF Run ID",         "adf_run_id",         38),
        ("Run Start (UTC)",    "run_start",          18),
        ("Run End (UTC)",      "run_end",            18),
        ("Failed Activity",    "failed_activities",  60),
        ("Error Message",      "root_cause",         80),
        ("Failed Step Input",  "failed_step_input",  70),
        ("Failed Step Output", "failed_step_output", 70),
        ("Status",             "status",             22),
    ]
    WRAP_FROM = {"Failed Activity", "Error Message", "Failed Step Input", "Failed Step Output", "Short Description"}

    if csv_rows:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "All Domain Job Failures"

        header_fill = PatternFill("solid", fgColor="1F497D")
        header_font = Font(color="FFFFFF", bold=True)
        fill_even   = PatternFill("solid", fgColor="DCE6F1")
        fill_odd    = PatternFill("solid", fgColor="FFFFFF")

        # Header row
        for col_idx, (label, _, width) in enumerate(EXCEL_HEADERS, start=1):
            cell = ws.cell(row=1, column=col_idx, value=label)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(col_idx)].width = width
        ws.row_dimensions[1].height = 30

        # Data rows
        for row_idx, row in enumerate(csv_rows, start=2):
            fill = fill_even if row_idx % 2 == 0 else fill_odd
            for col_idx, (label, field, _) in enumerate(EXCEL_HEADERS, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=row.get(field, ""))
                cell.fill = fill
                cell.alignment = Alignment(vertical="top", wrap_text=(label in WRAP_FROM))

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

        out_xlsx = logs_dir / f"incidents_to_adf_job_failures_{ts}.xlsx"
        wb.save(out_xlsx)
        print(f"\nOutput saved → {out_xlsx}  ({len(csv_rows)} rows)")

    # ── Save ADF errors JSON for HTML report enrichment ──────────────────────
    _snow_reports = Path(r"C:\Users\bgopu\servicenow-rpt\reports")
    if _snow_reports.exists():
        _adf_by_inc: dict = {}
        _seen_acts: dict = {}   # inc -> set of activity labels (dedup)
        for _row in csv_rows:
            _inc = _row.get("incident")
            if not _inc:
                continue
            if _inc not in _adf_by_inc:
                _adf_by_inc[_inc] = {
                    "pipeline":   _row.get("pipeline", ""),
                    "status":     _row.get("status", ""),
                    "root_cause": _row.get("root_cause", ""),  # fallback for no-activity rows
                    "activities": [],
                }
                _seen_acts[_inc] = set()
            # Keep the best (non-empty) root_cause at incident level
            if not _adf_by_inc[_inc]["root_cause"] and _row.get("root_cause"):
                _adf_by_inc[_inc]["root_cause"] = _row.get("root_cause", "")
            _act_label = _row.get("failed_activities", "")
            if _act_label and _act_label not in _seen_acts[_inc]:
                _seen_acts[_inc].add(_act_label)
                _adf_by_inc[_inc]["activities"].append({
                    "activity": _act_label,
                    "message":  _row.get("root_cause", ""),
                    "input":    _row.get("failed_step_input", ""),
                    "output":   _row.get("failed_step_output", ""),
                })
        _adf_json = dict(_adf_by_inc)
        _adf_path = _snow_reports / "adf_errors.json"
        with open(_adf_path, "w", encoding="utf-8") as _f:
            json.dump(_adf_json, _f, indent=2, ensure_ascii=False)
        print(f"💾 ADF errors JSON saved → {_adf_path}  ({len(_adf_json)} incidents)")

    print(f"\n{'='*80}")
    if skipped:
        print(f"Skipped {skipped} incident(s) with no job→pipeline mapping.")
    print(f"Correlation complete. {len(seen_run_ids)} unique ADF run(s) matched.")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    run_correlation(days)
