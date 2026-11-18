import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

import qpublic_master as qm  # noqa: E402
import qpublic_unit as qu  # noqa: E402
from qpublic_parser import parse_condo_units, is_condo_master, parse_qpublic_html

DEFAULT_DATA_DIR = "/home/kevinphan981/Programming/apartment-rental-scraper/data"
DEFAULT_INPUT = "apt.com-address-tmk.csv"


# --------------------------------------------------------------------------- keys
def to_parcel12(value):
    """'26021100.0' / 26021100 / '2-6-021-100' -> '260211000000'.  12-digit values pass through.
    Returns None if it can't be read as an 8- or 12-digit TMK."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return None
    s = re.sub(r"\.0+$", "", str(value).strip())  # drop the float artifact first
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return digits + "0000"
    if len(digits) == 12:
        return digits
    return None


def unit_key(unit_number):
    """Loose comparison key so apartments.com '0-2405' / '0301' match qpublic '2405' / '301'."""
    if unit_number is None or pd.isna(unit_number):
        return None
    k = re.sub(r"[^A-Z0-9]", "", str(unit_number).upper()).lstrip("0")
    return k or None


# --------------------------------------------------------------------------- cache
class Cache:
    """jsonl-backed {(kind, parcel): record}.  kind is 'building' or 'unit'."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            with open(self.path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        self.data[(r["kind"], r["parcel"])] = r
                    except (json.JSONDecodeError, KeyError):
                        continue

    def get_or_fetch(self, kind, parcel, fetch_fn, tag="", delay_sec=1.0):
        key = (kind, parcel)
        if key in self.data:
            print(f"{tag} SKIP {kind} {parcel} (cached)")
            return self.data[key]
        try:
            rec = fetch_fn(parcel)
        except Exception as e:
            print(f"{tag} FAIL {kind} {parcel}: {e}")
            return None
        rec.update({"kind": kind, "parcel": parcel})
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.data[key] = rec
        print(f"{tag} OK   {kind} {parcel}")
        time.sleep(delay_sec)
        return rec


# --------------------------------------------------------------------------- fetchers
def fetch_building(parcel12):
    """One request -> condo-unit grid AND the page's own tax record."""
    html = qm.fetch_master_html(parcel12)
    soup = BeautifulSoup(html, "lxml")
    units = parse_condo_units(soup)
    return {
        "is_condo_master": bool(is_condo_master(soup) or units),
        "units": units,
        "record": parse_qpublic_html(html),
    }


def fetch_unit(parcel12):
    return {"record": qu.fetch_and_parse_parcel(parcel12)}


# --------------------------------------------------------------------------- summarising
def summarize_tax(record):
    """Collapse one parcel's parsed record into a single flat dict (tax_* columns)."""
    out = {}
    if not record:
        return out
    out["tax_parcel_number"] = record.get("parcel_number")
    out["tax_location_address"] = record.get("location_address")
    out["tax_project_name"] = record.get("project_name")
    out["tax_current_property_class"] = record.get("current_property_class")

    status = record.get("tax_status_by_year") or []
    if status:
        latest = max(status, key=lambda d: d["tax_year"])
        out["tax_assessment_year"] = latest["tax_year"]
        out["tax_property_class"] = latest.get("property_class")
        out["tax_assessed_land"] = latest.get("assessed_land_value")
        out["tax_assessed_building"] = latest.get("assessed_building_value")
        out["tax_assessed_total"] = latest.get("total_assessed_value")
        out["tax_exemption"] = latest.get("total_exemption")
        out["tax_net_taxable"] = latest.get("total_net_taxable_value")
        out["tax_dedicated_use_value"] = latest.get("dedicated_use_value")
        out["tax_class_changes"] = sum(
            1 for d in status if d.get("property_class_changed_from_prior_year") is True
        )

    hist = record.get("historical_tax") or []
    if hist:
        latest = max(hist, key=lambda d: d["tax_year"])
        out["tax_bill_year"] = latest["tax_year"]
        out["tax_bill"] = latest.get("tax")
        out["tax_bill_amount_due"] = latest.get("amount_due")
        out["tax_total_amount_due_all_years"] = sum(
            (d.get("amount_due") or 0) for d in hist
        )
        out["tax_total_penalty_interest"] = sum(
            (d.get("penalty") or 0) + (d.get("interest") or 0) for d in hist
        )
        out["tax_years_with_balance"] = sum(
            1 for d in hist if (d.get("amount_due") or 0) > 0
        )

    ded = record.get("dedications_notice") or {}
    out["tax_dedications_has_no_data"] = ded.get("dedications_has_no_data")
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="listings CSV (relative to data-dir unless absolute)",
    )
    ap.add_argument("--output", default=None, help="default: <input>_with_tax.csv")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only process the first N buildings (testing)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    in_path = Path(args.input)
    if not in_path.is_absolute():
        in_path = data_dir / in_path
    out_path = (
        Path(args.output)
        if args.output
        else in_path.with_name(in_path.stem + "_with_tax.csv")
    )
    history_path = in_path.with_name(in_path.stem + "_tax_history.csv")
    cache = Cache(data_dir / "qpublic_cache.jsonl")

    listings = pd.read_csv(in_path, dtype=str)
    listings["tmk_parcel12"] = listings["TMK"].map(to_parcel12)
    bad = listings["tmk_parcel12"].isna().sum()
    if bad:
        print(
            f"WARNING: {bad} rows have a TMK that isn't 8 or 12 digits; they will get no tax data"
        )

    buildings = sorted(listings["tmk_parcel12"].dropna().unique())
    if args.limit:
        buildings = buildings[: args.limit]
    print(f"{len(listings)} listing rows, {len(buildings)} unique buildings")

    if args.dry_run:
        todo = [b for b in buildings if ("building", b) not in cache.data]
        print(
            f"Phase 1 would fetch {len(todo)} buildings ({len(buildings) - len(todo)} cached)."
        )
        print("Phase 2 (matched unit parcels) can only be sized after phase 1.")
        return

    # Phase 1: one fetch per building
    for i, b in enumerate(buildings, 1):
        cache.get_or_fetch(
            "building",
            b,
            fetch_building,
            tag=f"[{i}/{len(buildings)}]",
            delay_sec=args.delay,
        )

    # Phase 2: match listed units to unit parcels, fetch only those
    unit_map = {}  # building parcel12 -> {unit_key: unit parcel12}
    for b in buildings:
        rec = cache.data.get(("building", b))
        if not rec or not rec["units"]:
            continue
        m, dup = {}, set()
        for u in rec["units"]:
            k = unit_key(u.get("unit_number"))
            p = to_parcel12(u.get("parcel_number")) or u.get("parcel_number")
            if not k or not p:
                continue
            if k in m and m[k] != p:
                dup.add(k)
            m[k] = p
        for k in dup:
            m.pop(k)  # ambiguous -> don't guess
        unit_map[b] = m

    def match_unit(row):
        m = unit_map.get(row["tmk_parcel12"])
        k = unit_key(row.get("unit_number"))
        return m.get(k) if (m and k) else None

    listings["_unit_parcel"] = listings.apply(match_unit, axis=1)
    unit_parcels = sorted(listings["_unit_parcel"].dropna().unique())
    print(
        f"{listings['_unit_parcel'].notna().sum()} rows matched to {len(unit_parcels)} unit parcels"
    )
    for i, p in enumerate(unit_parcels, 1):
        cache.get_or_fetch(
            "unit",
            p,
            fetch_unit,
            tag=f"[{i}/{len(unit_parcels)}]",
            delay_sec=args.delay,
        )

    # Phase 3: merge
    summaries = {}
    for (kind, parcel), rec in cache.data.items():
        summaries[(kind, parcel)] = summarize_tax(rec["record"])

    rows = []
    for _, r in listings.iterrows():
        b, u = r["tmk_parcel12"], r["_unit_parcel"]
        has_unit_no = pd.notna(r.get("unit_number"))
        if isinstance(u, str) and ("unit", u) in summaries:
            level, s = "unit", summaries[("unit", u)]
        elif b and ("building", b) in summaries:
            brec = cache.data[("building", b)]
            s = summaries[("building", b)]
            if brec["is_condo_master"] and has_unit_no:
                level = "building_unit_not_matched"  # condo, but unit number didn't match any unit
            elif brec["is_condo_master"]:
                level = "building_condo_master"  # master parcel; values may not be meaningful
            else:
                level = "building"
        else:
            level, s = ("no_tmk" if not b else "fetch_failed"), {}
        rows.append({**s, "tax_match_level": level})

    out = pd.concat(
        [
            listings.drop(columns=["_unit_parcel"]).reset_index(drop=True),
            pd.DataFrame(rows),
        ],
        axis=1,
    )
    out.to_csv(out_path, index=False)

    # Long-form per-year history, one row per (parcel, tax_year)
    recs = [r["record"] for r in cache.data.values()]
    if recs:
        hist = qu.records_to_dataframe(recs)
        hist.to_csv(history_path, index=False)

    print(f"\nWrote {out_path}")
    print(f"Wrote {history_path}")
    print(out["tax_match_level"].value_counts().to_string())


if __name__ == "__main__":
    main()
CHECKPOINT_DIR = "/home/kevinphan981/Programming/apartment-rental-scraper/data"


def main():
    # Full batch run over every master TMK already in the CSV -- returns a DataFrame
    # directly (master_tmk, parcel_number, unit_number, modified_address), while
    # checkpointing progress to master_units.jsonl as it goes:
    masters = get_master_tmks_from_csv()
    df = fetch_many_masters(masters, out_path="master_units.jsonl")
    df.to_parquet(
        "data/master_units.parquet", index=False
    )  # optional, if you still want a file

    # we then fetch everything and then convert it into a dataframe (a new one)

    for parcel in df["parcel_number"]:
        fetch_and_parse_parcel(parcel)

    # we export to hopefully merge it with the apartments.com data later on.


if __name__ == "__main__":
    main()
