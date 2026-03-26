"""Enrich Incidents_list.csv with pipeline_name and subject_area from Autosys_Job_details_P01.xlsx."""
import pandas as pd
import shutil

INC_CSV   = "reports/Incidents_list.csv"
EXCEL     = "Autosys_Job_details_P01.xlsx"
SHEET     = "SummaryProd"
TMP_CSV   = "reports/Incidents_list_tmp.csv"

# Load incidents
inc = pd.read_csv(INC_CSV, low_memory=False)

# Drop any existing enriched columns to avoid duplication on re-runs
for col in ("pipeline_name", "subject_area"):
    if col in inc.columns:
        inc.drop(columns=[col], inplace=True)

# Load job -> pipeline mapping (all subject areas)
xl = pd.read_excel(EXCEL, sheet_name=SHEET)
mapping = (
    xl[["Autosys CMD", "Pipeline Name", "Subject Area"]]
    .dropna(subset=["Autosys CMD", "Pipeline Name"])
    .drop_duplicates(subset=["Autosys CMD"])
    .rename(columns={
        "Autosys CMD":   "job_key",
        "Pipeline Name": "pipeline_name",
        "Subject Area":  "subject_area",
    })
)
mapping["job_key"] = mapping["job_key"].str.strip().str.lower()

def extract_job(desc):
    if isinstance(desc, str) and "^" in desc:
        return desc.split("^")[0].strip().lower()
    return ""

inc["_job_key"] = inc["short_description"].apply(extract_job)
inc = inc.merge(
    mapping[["job_key", "pipeline_name", "subject_area"]],
    left_on="_job_key",
    right_on="job_key",
    how="left",
)
inc.drop(columns=["_job_key", "job_key"], inplace=True)

matched   = int(inc["pipeline_name"].notna().sum())
unmatched = int(inc["pipeline_name"].isna().sum())

inc.to_csv(TMP_CSV, index=False)
shutil.move(TMP_CSV, INC_CSV)

print(f"Enriched {matched} / {len(inc)} incidents with pipeline_name + subject_area")
print(f"Unmatched: {unmatched} (no job name or job not in mapping)")
print(f"Saved -> {INC_CSV}")
