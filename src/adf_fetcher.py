"""
ADF Pipeline Failure Fetcher

Queries Azure Data Factory for failed pipeline runs and their error details.
Can run standalone (lists recent failures) or be called programmatically
to correlate with ServiceNow incidents.

Usage:
    python adf_fetcher.py              # Show failures from last 7 days
    python adf_fetcher.py 30           # Show failures from last 30 days
    python adf_fetcher.py 7 my_pipeline  # Filter by pipeline name
"""

import json
import logging
import os
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Suppress Azure SDK credential warning noise (e.g. "AzureCliCredential not found")
logging.getLogger("azure.identity").setLevel(logging.ERROR)

from azure.identity import (
    AzureCliCredential,
    EnvironmentCredential,
    InteractiveBrowserCredential,
    ManagedIdentityCredential,
    TokenCachePersistenceOptions,
    ChainedTokenCredential,
    SharedTokenCacheCredential,
)
from azure.mgmt.datafactory import DataFactoryManagementClient
from azure.mgmt.datafactory.models import (
    RunFilterParameters,
    RunQueryFilter,
    RunQueryFilterOperand,
    RunQueryFilterOperator,
)

CONFIG_FILE = Path(__file__).parent.parent / "config" / "config_adf.json"

# Cache token credential across calls in one session
_credential = None


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_credential():
    """
    Credential chain (tried in order, first success wins):
    1. EnvironmentCredential   — Service Principal via env vars (best for Autosys/servers)
                                  Set: AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID
    2. ManagedIdentityCredential — Azure VM/container managed identity (no secrets needed)
    3. AzureCliCredential      — az login session (best for local dev)
    4. SharedTokenCache        — Windows token cache (reuses existing tokens silently)
    5. InteractiveBrowserCredential — one-time browser login, token cached to disk
    """
    global _credential
    if _credential is not None:
        return _credential

    # 1. Service Principal via environment variables (Autosys / CI / remote server)
    if all(os.environ.get(v) for v in ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")):
        try:
            cred = EnvironmentCredential()
            cred.get_token("https://management.azure.com/.default")
            print("[Auth] Using Service Principal (EnvironmentCredential).")
            _credential = cred
            return cred
        except Exception:
            pass

    # 2. Managed Identity (Azure VM, ACI, App Service — no credentials needed)
    try:
        cred = ManagedIdentityCredential()
        cred.get_token("https://management.azure.com/.default")
        print("[Auth] Using Managed Identity.")
        _credential = cred
        return cred
    except Exception:
        pass

    # 3. Azure CLI (az login) — silent, best for local scheduled runs
    try:
        cred = AzureCliCredential()
        cred.get_token("https://management.azure.com/.default")
        print("[Auth] Using Azure CLI credentials.")
        _credential = cred
        return cred
    except Exception:
        pass

    # 4. Windows shared token cache (reuses previously cached tokens silently)
    try:
        cred = SharedTokenCacheCredential()
        cred.get_token("https://management.azure.com/.default")
        print("[Auth] Using Windows shared token cache.")
        _credential = cred
        return cred
    except Exception:
        pass

    # 5. Fall back to interactive browser with persistent disk cache
    print("[Auth] Opening browser for login (one-time -- token will be cached)...")
    cache_opts = TokenCachePersistenceOptions(name="servicenow-rpt-adf", allow_unencrypted_storage=True)
    cred = InteractiveBrowserCredential(cache_persistence_options=cache_opts)
    _credential = cred
    return cred


def get_adf_client(config: dict) -> DataFactoryManagementClient:
    return DataFactoryManagementClient(get_credential(), config["subscription_id"])


def get_failed_runs(config: dict, days_back: int = 7, retries: int = 3, backoff: float = 5.0) -> tuple[list, DataFactoryManagementClient]:
    """
    Fetch all failed pipeline runs in the last `days_back` days.
    Retries on transient SSL/network errors up to `retries` times.
    Returns (runs_list, client) so the client can be reused for activity queries.
    """
    client = get_adf_client(config)

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)

    filter_params = RunFilterParameters(
        last_updated_after=start,
        last_updated_before=now,
        filters=[
            RunQueryFilter(
                operand=RunQueryFilterOperand.STATUS,
                operator=RunQueryFilterOperator.EQUALS,
                values=["Failed"],
            )
        ],
    )

    _TRANSIENT = (ssl.SSLError, ConnectionError, TimeoutError, OSError)

    for attempt in range(1, retries + 1):
        try:
            result = client.pipeline_runs.query_by_factory(
                config["resource_group"], config["adf_name"], filter_params
            )
            runs = list(result.value)
            while result.continuation_token:
                filter_params.continuation_token = result.continuation_token
                result = client.pipeline_runs.query_by_factory(
                    config["resource_group"], config["adf_name"], filter_params
                )
                runs.extend(result.value)
            return runs, client
        except _TRANSIENT as e:
            if attempt < retries:
                wait = backoff * attempt
                print(f"    ⚠ Network error (attempt {attempt}/{retries}): {e} — retrying in {wait:.0f}s...")
                time.sleep(wait)
            else:
                raise


def get_activity_errors(
    client: DataFactoryManagementClient,
    config: dict,
    run_id: str,
    _depth: int = 0,
    _breadcrumb: str = "",
    _parent_run_id: str = "",
) -> list[dict]:
    """
    Recursively fetch leaf-level failed activity errors for a pipeline run.
    Drills into ExecutePipeline (child run) and ForEach (inner activities) wrappers
    to return only the specific technical error at the point of failure.
    """
    # Wrapper activity types that contain child activities — skip their own error message
    WRAPPER_TYPES = {"ExecutePipeline", "ForEach", "IfCondition", "Until", "Switch"}
    # Wrapper error codes that are just re-raising a child error
    WRAPPER_ERROR_CODES = {"ActionFailed", "BadRequest", ""}

    if _depth > 5:
        return []  # Guard against infinite recursion

    filter_params = RunFilterParameters(
        last_updated_after=datetime(2020, 1, 1, tzinfo=timezone.utc),
        last_updated_before=datetime.now(timezone.utc),
    )

    try:
        result = client.activity_runs.query_by_pipeline_run(
            config["resource_group"], config["adf_name"], run_id, filter_params
        )
    except Exception as e:
        return [{"activity": _breadcrumb or run_id, "type": "Unknown",
                 "error_code": "FetchError", "message": str(e), "failure_type": "", "input": "", "output": "",
                 "parent_run_id": _parent_run_id, "child_run_id": run_id}]

    # Collect ALL pages — ForEach with many failed iterations can span multiple pages
    all_acts = list(result.value)
    while result.continuation_token:
        filter_params.continuation_token = result.continuation_token
        try:
            result = client.activity_runs.query_by_pipeline_run(
                config["resource_group"], config["adf_name"], run_id, filter_params
            )
            all_acts.extend(result.value)
        except Exception:
            break

    errors = []
    for act in all_acts:
        if act.status != "Failed":
            continue

        err = act.error or {}
        error_code = err.get("errorCode", "")
        act_type = act.activity_type or ""
        breadcrumb = f"{_breadcrumb} > {act.activity_name}" if _breadcrumb else act.activity_name

        # --- ExecutePipeline: drill into the child pipeline run ---
        if act_type == "ExecutePipeline":
            child_run_id = None
            output = act.output or {}
            child_run_id = output.get("pipelineRunId")
            if child_run_id:
                child_errors = get_activity_errors(
                    client, config, child_run_id, _depth + 1, breadcrumb,
                    _parent_run_id=run_id,
                )
                errors.extend(child_errors)
                continue
            # If we can't get child run ID, fall through to report what we have

        # --- ForEach / other wrappers: the inner activities are in the same run ---
        # They appear as separate activity run records already; skip the wrapper's own error
        if act_type in WRAPPER_TYPES and error_code in WRAPPER_ERROR_CODES:
            continue

        # --- Leaf activity: real error ---
        # Capture input / output (serialize dict/list to compact JSON string)
        import json as _json

        raw_input = act.input or {}
        if isinstance(raw_input, (dict, list)):
            input_str = _json.dumps(raw_input, ensure_ascii=False, separators=(",", ":"))
        else:
            input_str = str(raw_input)

        raw_output = act.output or {}
        if isinstance(raw_output, (dict, list)):
            output_str = _json.dumps(raw_output, ensure_ascii=False, separators=(",", ":"))
        else:
            output_str = str(raw_output)

        errors.append(
            {
                "activity":      breadcrumb,
                "type":          act_type,
                "error_code":    error_code,
                "message":       err.get("message", ""),
                "failure_type":  err.get("failureType", ""),
                "input":         input_str,
                "output":        output_str,
                "parent_run_id": _parent_run_id,
                "child_run_id":  run_id,
            }
        )
    return errors


def find_runs_for_incident(
    runs: list,
    pipeline_name_hint: str,
    incident_time,
    window_hours: float = 2.0,
) -> list:
    """
    Return ADF runs that match a ServiceNow incident by pipeline name and timing.

    Args:
        runs: list of pipeline run objects from get_failed_runs()
        pipeline_name_hint: partial pipeline name extracted from incident description
        incident_time: datetime or ISO string of when the incident was opened
        window_hours: how many hours before/after the incident to search
    """
    if isinstance(incident_time, str):
        incident_time = datetime.fromisoformat(incident_time.replace("Z", "+00:00"))
    if incident_time.tzinfo is None:
        incident_time = incident_time.replace(tzinfo=timezone.utc)

    hint_lower = pipeline_name_hint.lower() if pipeline_name_hint else ""

    matches = []
    for run in runs:
        # Name match (partial, case-insensitive)
        if hint_lower:
            name_lower = run.pipeline_name.lower()
            if hint_lower not in name_lower and name_lower not in hint_lower:
                continue

        # Timing match
        run_start = run.run_start
        if run_start is None:
            continue
        if run_start.tzinfo is None:
            run_start = run_start.replace(tzinfo=timezone.utc)

        delta_hours = abs((run_start - incident_time).total_seconds() / 3600)
        if delta_hours <= window_hours:
            matches.append(run)

    return matches


def _extract_root_cause(message: str) -> str:
    """
    Pull the deepest real error out of ADF's nested 'Operation on target X failed: ...' chain.
    Returns the innermost Error/Message content, untruncated.
    """
    if not message:
        return message
    # Find the last 'Error:' segment — that's the leaf error
    last_error_idx = message.rfind(", Error: ")
    if last_error_idx != -1:
        return message[last_error_idx + 9:]
    # Fallback: strip leading wrapper text up to "ErrorCode="
    ec_idx = message.find("ErrorCode=")
    if ec_idx != -1:
        return message[ec_idx:]
    return message


def format_run_summary(run, errors: list[dict] | None = None) -> str:
    """Return a readable text block for a pipeline run."""
    lines = [
        f"  Pipeline : {run.pipeline_name}",
        f"  Run ID   : {run.run_id}",
        f"  Started  : {run.run_start}",
        f"  Ended    : {run.run_end}",
        f"  Status   : {run.status}",
    ]

    # Show which leaf activities failed (breadcrumb path from recursive drilling)
    if errors:
        seen_activities = set()
        for e in errors:
            act_key = e["activity"]
            if act_key not in seen_activities:
                seen_activities.add(act_key)
                lines.append(f"  Failed Activity : {e['activity']} [{e['type']}]")
                if e["error_code"]:
                    lines.append(f"    ErrCode  : {e['error_code']}")
                if e["failure_type"]:
                    lines.append(f"    FailType : {e['failure_type']}")

    # Full untruncated root cause from pipeline run message (API never truncates this field)
    if run.message:
        root = _extract_root_cause(str(run.message))
        lines.append(f"  Root Cause : {root}")

    return "\n".join(lines)


def main():
    config = load_config()

    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    pipeline_filter = sys.argv[2].lower() if len(sys.argv) > 2 else None

    print(f"ADF: {config['adf_name']}  |  RG: {config['resource_group']}")
    print(f"Fetching failed runs — last {days_back} day(s)...\n")

    runs, client = get_failed_runs(config, days_back)

    if pipeline_filter:
        runs = [r for r in runs if pipeline_filter in r.pipeline_name.lower()]

    if not runs:
        print("No failed pipeline runs found.")
        return

    # Sort newest first
    runs.sort(
        key=lambda r: r.run_start or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    print(f"Found {len(runs)} failed run(s):\n")
    for i, run in enumerate(runs, 1):
        print(f"[{i}] {'-'*60}")
        errors = get_activity_errors(client, config, run.run_id)
        print(format_run_summary(run, errors))

    print()


if __name__ == "__main__":
    main()
