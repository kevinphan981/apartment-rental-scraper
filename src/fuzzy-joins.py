r"""
Two-stage building + unit matching between master_units.parquet and
condo_listings.csv -- STANDARDIZED-ADDRESS version.

Why this replaces the rapidfuzz approach:
- rapidfuzz needed a hand-tuned `STREET_REPLACEMENTS` dict and a cutoff score,
  and still only ever gives you a *probabilistic* match.
- `usaddress` is a CRF address parser trained on Census Bureau / OpenAddresses
  data. `scourgify` sits on top of it and emits a full USPS-standardized
  address (suffixes abbreviated per the USPS C1 table, unit split out).
- Once both sides are standardized the same way, street matching becomes an
  ordinary EXACT join -- no fuzzy scores, no cutoff to tune, no false
  positives from two similar-but-different streets scoring above threshold.

Coverage check on your actual data (see chat) before adopting this:
  - master_units: 5,824 rows -> only 6 fail to parse (all parking-stall
    units like "APT PKG ST 78"; not real rentable units anyway).
  - condo_listings: 653 rows -> only 2 fail to parse (both scraper garbage
    where building_name itself is corrupted, e.g. "...Unit Tropical Living
    Room"). Everything else standardizes cleanly and deterministically.

Both failure modes are logged to `unparsed_master.csv` / `unparsed_listings.csv`
for manual review rather than silently dropped.
"""

import re
import pandas as pd

from scourgify import normalize_address_record
from scourgify.exceptions import AddressNormalizationError

# ---------- Load ----------
df = pd.read_parquet("data/master_units.parquet")
cl = pd.read_csv("data/condo_listings.csv")

df["master_tmk"] = df["master_tmk"].astype("int64")
df["parcel_number"] = df["parcel_number"].astype("int64")

# ---------- Stage 0: standardize every address in one pass ----------
# scourgify returns {'address_line_1': '<STD STREET>', 'address_line_2':
# '<STD UNIT>' or None, ...}. This replaces split_master/split_listing AND
# the slugify step in one call -- street and unit come out already separated
# and already normalized to USPS abbreviations.


def standardize(addr):
    """Return (street_std, unit_std) or (None, None) if unparseable/blank."""
    if pd.isna(addr):
        return None, None
    try:
        rec = normalize_address_record(str(addr))
        return rec["address_line_1"], rec["address_line_2"]
    except AddressNormalizationError:
        return None, None
    except Exception:
        # usaddress raises its own RepeatedLabelError (not a scourgify
        # exception) on genuinely malformed strings -- treat the same way.
        return None, None


df[["street_std", "unit_std"]] = df["modified_address"].apply(
    lambda a: pd.Series(standardize(a))
)
cl[["street_std", "unit_std"]] = cl["address"].apply(
    lambda a: pd.Series(standardize(a))
)

# Log what didn't parse so it can be fixed by hand instead of vanishing.
unparsed_master = df[df["modified_address"].notna() & df["street_std"].isna()]
unparsed_listings = cl[cl["address"].notna() & cl["street_std"].isna()]
unparsed_master[["master_tmk", "parcel_number", "modified_address"]].to_csv(
    "data/unparsed_master.csv", index=False
)
unparsed_listings[["listing_id", "address", "building_name"]].to_csv(
    "data/unparsed_listings.csv", index=False
)

# sanity check: every master_tmk should collapse to exactly one street_std
n_fragmented = (
    df.loc[df["street_std"].notna()]
    .groupby("master_tmk")["street_std"]
    .nunique()
    .gt(1)
    .sum()
)
assert n_fragmented == 0, f"{n_fragmented} buildings still fragmented after standardization"

master_buildings = df.loc[df["street_std"].notna(), ["master_tmk", "street_std"]].drop_duplicates()
listing_buildings = cl.loc[cl["street_std"].notna(), ["building_name", "street_std"]].drop_duplicates()

print(f"Unique master buildings:  {master_buildings['master_tmk'].nunique()}")
print(f"Unique listing buildings: {listing_buildings['street_slug' if False else 'street_std'].nunique()}")

# ---------- Stage 1: EXACT join, street <-> street ----------
# No scorer, no cutoff -- both sides are already in the same standardized
# form, so equal strings mean the same building.
street_to_master = master_buildings.drop_duplicates(subset="street_std").set_index("street_std")["master_tmk"]

cl["matched_master_tmk"] = cl["street_std"].map(street_to_master)

n_matched_buildings = cl["matched_master_tmk"].notna().sum()
print(f"Listing rows matched to a master building (exact): {n_matched_buildings} / {len(cl)}")

# ---------- Stage 2: attach real unit numbers within a matched building ----------
# scourgify's address_line_2 already comes back as e.g. "APT 1401" / "UNIT
# 1100" -- strip the leading label and non-alphanumerics for the join key.
# Scraper placeholder units ("SI FL16-ID1327703P", "SI ID1608459P", ...)
# are flagged as junk the same way as before, since they can never match a
# real assessor unit number.
JUNK_UNIT_RE = re.compile(r"^SI\s*(FL\d+-)?ID\d+P?$", flags=re.IGNORECASE)


def unit_key(unit_std):
    if pd.isna(unit_std) or unit_std is None:
        return None
    u = re.sub(r"^(APT|UNIT|STE|NO)\s*", "", str(unit_std).upper()).strip()
    if not u or JUNK_UNIT_RE.match(u.replace(" ", "")):
        return None
    return re.sub(r"[^A-Z0-9-]", "", u)


df["unit_key"] = df["unit_std"].apply(unit_key)
cl["unit_key"] = cl["unit_std"].apply(unit_key)
cl["unit_is_real"] = cl["unit_key"].notna()

# master lookup keyed by (street_std, unit_key) for O(1) exact unit lookups
master_unit_lookup = df.set_index(["street_std", "unit_key"])["parcel_number"].to_dict()


def find_unit_match(row):
    if pd.isna(row["matched_master_tmk"]) or not row["unit_is_real"]:
        return None
    return master_unit_lookup.get((row["street_std"], row["unit_key"]))


cl["matched_parcel_number"] = cl.apply(find_unit_match, axis=1)

# ---------- Report ----------
print()
print(f"Listing rows landing in a matched building:       {cl['matched_master_tmk'].notna().sum()} / {len(cl)}")
print(f"Listing rows with a real (non-junk) unit number:   {cl['unit_is_real'].sum()} / {len(cl)}")
print(f"...of which we found an EXACT master unit match:   {cl['matched_parcel_number'].notna().sum()}")
print()
print(f"Unparsed master rows (see unparsed_master.csv):     {len(unparsed_master)}")
print(f"Unparsed listing rows (see unparsed_listings.csv):  {len(unparsed_listings)}")

# ---------- Output: what downstream (qpublic scrape) cell should consume ----------
scrape_targets = (
    cl.loc[cl["matched_parcel_number"].notna(), ["matched_parcel_number"]]
    .rename(columns={"matched_parcel_number": "parcel_number"})
    .drop_duplicates()
    .merge(df[["parcel_number", "master_tmk", "unit_key", "modified_address"]], on="parcel_number", how="left")
)
print()
print(f"Unique parcels ready for qpublic scrape: {len(scrape_targets)}") # 28 units... 
print(scrape_targets.head().to_string())