
import argparse
import json
import os
import pandas as pd
 
from master_tmk_search import MasterTmkIndex
 
 
def load_records(path: str) -> list:
    """Load one JSON record per line (jsonl) OR a single JSON array file."""
    records = []
    with open(path, "r") as f:
        first_char = f.read(1)
        f.seek(0)
        if first_char == "[":
            records = json.load(f)
        else:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records
 
 
def build_parcels_table(records: list) -> pd.DataFrame:
    rows = []
    for r in records:
        rows.append({
            "parcel_number": r.get("parcel_number"),
            "location_address": r.get("location_address"),
            "project_name": r.get("project_name"),
            "legal_information": r.get("legal_information"),
            "current_property_class": r.get("current_property_class"),
            "dedications_has_no_data": (r.get("dedications_notice") or {}).get("dedications_has_no_data"),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["parcel_number"] = df["parcel_number"].astype(str).str.zfill(12)
    return df
 
 
def build_tax_status_table(records: list) -> pd.DataFrame:
    rows = []
    for r in records:
        parcel_number = r.get("parcel_number")
        for year_row in r.get("tax_status_by_year", []):
            rows.append({"parcel_number": parcel_number, **year_row})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["parcel_number"] = df["parcel_number"].astype(str).str.zfill(12)
    return df
 
 
def build_historical_tax_table(records: list) -> pd.DataFrame:
    rows = []
    for r in records:
        parcel_number = r.get("parcel_number")
        for year_row in r.get("historical_tax", []):
            row = {k: v for k, v in year_row.items() if k != "payments"}
            rows.append({"parcel_number": parcel_number, **row})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["parcel_number"] = df["parcel_number"].astype(str).str.zfill(12)
    return df
 
 
def build_payments_table(records: list) -> pd.DataFrame:
    rows = []
    for r in records:
        parcel_number = r.get("parcel_number")
        for year_row in r.get("historical_tax", []):
            tax_year = year_row.get("tax_year")
            for payment in year_row.get("payments", []):
                rows.append({
                    "parcel_number": parcel_number,
                    "tax_year": tax_year,
                    **payment,
                })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["parcel_number"] = df["parcel_number"].astype(str).str.zfill(12)
    return df
 
 
def enrich_parcels_with_master_file(parcels_df: pd.DataFrame, master_data_path: str) -> pd.DataFrame:
    """Left-join the auxiliary manual-tmk-data.ods columns onto parcels_df.
    Uses whatever columns MasterTmkIndex exposes (works for either the ODS
    per-building-sheet layout or the legacy flat CSV layout), so this doesn't
    need to change if the source file's available columns change."""
    idx = MasterTmkIndex.load(master_data_path)
    aux_cols = [c for c in idx.df.columns if c != "parcel_number"]
    master_subset = idx.df[["parcel_number"] + aux_cols].drop_duplicates("parcel_number")
 
    merged = parcels_df.merge(master_subset, on="parcel_number", how="left")
    return merged
 
 
def build_wide_year_table(tax_status_df: pd.DataFrame, historical_tax_df: pd.DataFrame) -> pd.DataFrame:
    """Optional convenience: one row per (parcel_number, tax_year) combining
    property-class/dedication info with the tax billing/payment summary."""
    if tax_status_df.empty or historical_tax_df.empty:
        return pd.DataFrame()
    return tax_status_df.merge(
        historical_tax_df, on=["parcel_number", "tax_year"], how="outer",
        suffixes=("_assessment", "_billing"),
    )
 
 
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", required=True,
                         help="Path to jsonl (or JSON array) file of parsed qpublic records")
    parser.add_argument("--master-data", default="/mnt/user-data/uploads/manual-tmk-data.ods",
                         help="Path to manual-tmk-data.ods (or a legacy flat CSV)")
    parser.add_argument("--out-dir", default="/mnt/user-data/outputs")
    args = parser.parse_args()
 
    records = load_records(args.records)
    print(f"Loaded {len(records)} parcel record(s)")
 
    parcels_df = build_parcels_table(records)
    parcels_df = enrich_parcels_with_master_file(parcels_df, args.master_data)
    tax_status_df = build_tax_status_table(records)
    historical_tax_df = build_historical_tax_table(records)
    payments_df = build_payments_table(records)
 
    os.makedirs(args.out_dir, exist_ok=True)
 
    out_paths = {
        "parcels.csv": parcels_df,
        "tax_status_by_year.csv": tax_status_df,
        "historical_tax.csv": historical_tax_df,
        "tax_payments.csv": payments_df,
    }
    for filename, df in out_paths.items():
        path = f"{args.out_dir.rstrip('/')}/{filename}"
        df.to_csv(path, index=False)
        print(f"  wrote {path}  ({len(df)} rows, {len(df.columns)} cols)")
 
 
if __name__ == "__main__":
    main()
 