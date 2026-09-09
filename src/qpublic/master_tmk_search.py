
import argparse
import re
import pandas as pd
 
README_SHEET_NAME = "README"
 
 
def _zero_pad_12(series: pd.Series) -> pd.Series:
    """TMK/parcel values are always 12 digits (Honolulu format); keep them as
    zero-padded strings everywhere so nothing is ever silently truncated or
    treated as a number (e.g. dropped leading zeros, float coercion, etc.)."""
    return series.astype("Int64").astype(str).str.zfill(12)
 
 
def _normalize_name(name: str) -> str:
    """Loosely normalize a building name for matching a per-building sheet name
    against the README's CONDOMINIUM PROJECT NAME -- sheet names can't contain
    characters like '*' that appear in some project names, and casing/whitespace
    can drift between the two."""
    if not isinstance(name, str):
        return ""
    return re.sub(r"[^A-Z0-9]", "", name.upper())
 
 
class MasterTmkIndex:
    def __init__(self, df: pd.DataFrame, missing_sheets: list = None):
        self.df = df
        self._missing_sheets = missing_sheets or []
 
    @classmethod
    def load(cls, path: str) -> "MasterTmkIndex":
        if str(path).lower().endswith((".ods", ".xlsx", ".xls")):
            return cls._load_workbook(path)
        return cls._load_flat_csv(path)
 
    @classmethod
    def _load_workbook(cls, path: str) -> "MasterTmkIndex":
        engine = "odf" if str(path).lower().endswith(".ods") else None
        xl = pd.ExcelFile(path, engine=engine)
 
        if README_SHEET_NAME not in xl.sheet_names:
            raise ValueError(f"No '{README_SHEET_NAME}' sheet found in {path}")
 
        readme = xl.parse(README_SHEET_NAME)
        readme["CONDO MASTER TMK"] = _zero_pad_12(readme["CONDO MASTER TMK"])
        readme["_match_key"] = readme["CONDOMINIUM PROJECT NAME"].map(_normalize_name)
 
        building_sheets = [s for s in xl.sheet_names if s != README_SHEET_NAME]
        sheet_match_keys = {_normalize_name(s): s for s in building_sheets}
 
        rows = []
        matched_sheets = set()
        for _, building in readme.iterrows():
            sheet_name = sheet_match_keys.get(building["_match_key"])
            if sheet_name is None:
                continue  # no per-unit sheet for this building yet
            matched_sheets.add(sheet_name)
 
            units = xl.parse(sheet_name)
            units = units.rename(columns={
                "Parcel Number": "parcel_number",
                "Unit Number": "unitno",
                "Owner Name": "owner_name",
            })
            units["parcel_number"] = _zero_pad_12(units["parcel_number"])
            units["CONDOMINIUM PROJECT NAME"] = building["CONDOMINIUM PROJECT NAME"]
            units["SITE ADDRESS"] = building["SITE ADDRESS"]
            units["CONDO MASTER TMK"] = building["CONDO MASTER TMK"]
            units["PROPERTY ZONE"] = building["PROPERTY ZONE"]
            units["Source"] = building.get("Source")
            rows.append(units)
 
        df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
            columns=["parcel_number", "unitno", "owner_name", "CONDOMINIUM PROJECT NAME",
                     "SITE ADDRESS", "CONDO MASTER TMK", "PROPERTY ZONE", "Source"]
        )
 
        missing = [s for s in building_sheets if s not in matched_sheets]
        unmatched_readme = readme[~readme["CONDOMINIUM PROJECT NAME"].isin(
            df["CONDOMINIUM PROJECT NAME"].unique()
        )]["CONDOMINIUM PROJECT NAME"].tolist()
 
        idx = cls(df, missing_sheets=unmatched_readme)
        idx.readme = readme.drop(columns=["_match_key"])
        idx._unmatched_building_sheets = missing  # sheets present but not in README (unexpected)
        return idx
 
    @classmethod
    def _load_flat_csv(cls, path: str) -> "MasterTmkIndex":
        """Backward-compat loader for the older flat master_tmk_address.csv layout."""
        df = pd.read_csv(path)
        df = df.rename(columns={"parid": "parcel_number"})
        df["parcel_number"] = _zero_pad_12(df["parcel_number"])
        df["CONDO MASTER TMK"] = _zero_pad_12(df["CONDO MASTER TMK"])
        return cls(df)
 
    def list_master_tmks(self) -> pd.DataFrame:
        """One row per building: master TMK, project name, address, unit count.
        Buildings from README with no matching sheet yet show unit_count=0."""
        if not self.df.empty:
            have_units = (
                self.df.groupby("CONDO MASTER TMK")
                .agg(
                    project_name=("CONDOMINIUM PROJECT NAME", "first"),
                    site_address=("SITE ADDRESS", "first"),
                    property_zone=("PROPERTY ZONE", "first"),
                    unit_count=("parcel_number", "count"),
                )
                .reset_index()
            )
        else:
            have_units = pd.DataFrame(columns=[
                "CONDO MASTER TMK", "project_name", "site_address", "property_zone", "unit_count"
            ])
 
        if hasattr(self, "readme"):
            readme_summary = self.readme.rename(columns={
                "CONDOMINIUM PROJECT NAME": "project_name",
                "SITE ADDRESS": "site_address",
                "PROPERTY ZONE": "property_zone",
            })[["CONDO MASTER TMK", "project_name", "site_address", "property_zone"]]
            summary = readme_summary.merge(
                have_units[["CONDO MASTER TMK", "unit_count"]],
                on="CONDO MASTER TMK", how="left"
            )
            summary["unit_count"] = summary["unit_count"].fillna(0).astype(int)
        else:
            summary = have_units
 
        return summary.sort_values("unit_count", ascending=False).reset_index(drop=True)
 
    def get_parcels_for_master_tmk(self, master_tmk: str) -> pd.DataFrame:
        """All individual unit rows for one building, given its master TMK."""
        master_tmk = str(master_tmk).zfill(12)
        return self.df[self.df["CONDO MASTER TMK"] == master_tmk].reset_index(drop=True)
 
    def get_parcel_numbers(self, master_tmk) -> list:
        """
        Flat list of parcel numbers (zero-padded 12-digit strings) for one
        master TMK, or several. Pass a single master TMK, or a list/iterable of
        them. This is the exact input fetch_many() in scrapfly-qpublic-fetch.py
        expects. Returns an empty list if that building has no unit sheet yet.
        """
        if isinstance(master_tmk, (list, tuple, set, pd.Series)):
            masters = {str(m).zfill(12) for m in master_tmk}
            subset = self.df[self.df["CONDO MASTER TMK"].isin(masters)]
        else:
            subset = self.get_parcels_for_master_tmk(master_tmk)
        return subset["parcel_number"].tolist()
 
    def search_by_building_name(self, name_substring: str) -> pd.DataFrame:
        mask = self.df["CONDOMINIUM PROJECT NAME"].str.contains(
            name_substring, case=False, na=False
        )
        return self.df[mask].reset_index(drop=True)
 
    def search_by_address(self, address_substring: str) -> pd.DataFrame:
        mask = self.df["SITE ADDRESS"].str.contains(
            address_substring, case=False, na=False
        )
        return self.df[mask].reset_index(drop=True)
 
    def all_master_tmks(self) -> list:
        """Every distinct master TMK with at least one unit loaded, as zero-padded strings."""
        return sorted(self.df["CONDO MASTER TMK"].unique().tolist())
 
    def buildings_without_unit_sheet(self) -> list:
        """Building names listed in README that don't have a per-unit sheet yet
        (i.e. still need to be manually filled in)."""
        return self._missing_sheets
 
 
def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="/mnt/user-data/uploads/manual-tmk-data.ods",
                         help="Path to manual-tmk-data.ods (or a legacy flat CSV)")
    parser.add_argument("--list-masters", action="store_true",
                         help="Print a summary of every master TMK / building")
    parser.add_argument("--master-tmk", help="Look up units under one master TMK")
    parser.add_argument("--building", help="Fuzzy-search by condo project name")
    parser.add_argument("--address", help="Fuzzy-search by site address")
    parser.add_argument("--parcel-numbers-only", action="store_true",
                         help="With --master-tmk, print just the parcel number list (one per line)")
    parser.add_argument("--missing-sheets", action="store_true",
                         help="List README buildings that don't have a per-unit sheet yet")
    args = parser.parse_args()
 
    idx = MasterTmkIndex.load(args.data)
 
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
 
    if args.list_masters:
        print(idx.list_master_tmks().to_string(index=False))
    elif args.missing_sheets:
        missing = idx.buildings_without_unit_sheet()
        if missing:
            print(f"{len(missing)} building(s) in README with no unit sheet yet:")
            for b in missing:
                print(f"  - {b}")
        else:
            print("Every building in README has a matching unit sheet.")
    elif args.master_tmk:
        if args.parcel_numbers_only:
            for p in idx.get_parcel_numbers(args.master_tmk):
                print(p)
        else:
            cols = ["parcel_number", "unitno", "owner_name", "SITE ADDRESS", "CONDOMINIUM PROJECT NAME"]
            cols = [c for c in cols if c in idx.df.columns]
            print(idx.get_parcels_for_master_tmk(args.master_tmk)[cols].to_string(index=False))
    elif args.building:
        cols = ["parcel_number", "CONDOMINIUM PROJECT NAME", "SITE ADDRESS", "CONDO MASTER TMK"]
        cols = [c for c in cols if c in idx.df.columns]
        print(idx.search_by_building_name(args.building)[cols].to_string(index=False))
    elif args.address:
        cols = ["parcel_number", "CONDOMINIUM PROJECT NAME", "SITE ADDRESS", "CONDO MASTER TMK"]
        cols = [c for c in cols if c in idx.df.columns]
        print(idx.search_by_address(args.address)[cols].to_string(index=False))
    else:
        parser.print_help()
 
 
if __name__ == "__main__":
    _main()