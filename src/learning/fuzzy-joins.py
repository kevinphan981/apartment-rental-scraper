r"""
Two-stage building + unit matching between master_units.parquet and
condo_listings.csv.

Why two stages, not one fuzzy match on the full address:
- master_units addresses are ALWAYS unit-level: "<STREET> APT <UNIT>"
- condo_listings addresses are a MIX: building-only, real unit numbers,
  or scraper-junk pseudo-units like "Unit SI FL6-ID1327716P"
Comparing full strings mixes these granularities and produces both false
negatives (real matches scored too low) and meaningless high scores.

Fix: split each address into (street, unit) FIRST, fuzzy-match streets only
(apples to apples), then exact-match real unit numbers within a matched
building.

IMPORTANT bug this version fixes vs. the first draft: the unit token in
master_units can contain SPACES ("APT PARKING 3", "APT SKYROOM 30",
"APT AOAO 206", "APT LBBY LL1"), so a naive `(\S+)` capture at the end of
the regex silently failed on ~400/5824 rows and left the unit still glued
onto "street", fragmenting single buildings into several fake ones.
Splitting on the literal " APT " marker and taking everything after it
(spaces included) as the unit fixes this.
"""

import re
import pandas as pd
import os

from rapidfuzz import fuzz, process

# ---------- Load ----------
os.chdir('/home/kevinphan981/Programming/apartment-rental-scraper/')

df = pd.read_parquet('data/master_units.parquet')
cl = pd.read_csv('data/condo_listings.csv')

df['master_tmk'] = df['master_tmk'].astype('int64')
df['parcel_number'] = df['parcel_number'].astype('int64')

# ---------- Stage 0: split each address into (street_raw, unit_raw) ----------

def split_master(addr):
    """Master format: '<STREET> APT <UNIT...>'. Unit may itself contain
    spaces, so split on the marker, don't try to capture a single token."""
    if pd.isna(addr):
        return None, None
    parts = re.split(r"\sAPT\s+", str(addr), maxsplit=1, flags=re.IGNORECASE)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], None)


def split_listing(addr):
    """Listing format: '<STREET>' or '<STREET>[.] Unit <UNIT...>'."""
    if pd.isna(addr):
        return None, None
    m = re.match(r"^(.*?)\.?\s+Unit\s+(.+)$", str(addr), flags=re.IGNORECASE)
    return (m.group(1), m.group(2)) if m else (addr, None)


df[['street_raw', 'unit_raw']] = df['modified_address'].apply(lambda a: pd.Series(split_master(a)))
cl[['street_raw', 'unit_raw']] = cl['address'].apply(lambda a: pd.Series(split_listing(a)))

# Flag scraper-junk "units" that aren't real apartment numbers, e.g.
# "SI FL6-ID1327716P" -- these can never be exact-matched to a real unit.
JUNK_UNIT_RE = re.compile(r"^SI\s*FL\d+-ID\d+P$", flags=re.IGNORECASE)
cl['unit_is_real'] = cl['unit_raw'].apply(
    lambda u: bool(u) and not JUNK_UNIT_RE.match(str(u).strip())
)

# ---------- Stage 1: slugify STREET ONLY on both sides ----------
STREET_REPLACEMENTS = {
    r"\bstreet\b": "st", r"\broad\b": "rd", r"\bavenue\b": "ave",
    r"\bboulevard\b": "blvd", r"\bdrive\b": "dr", r"\blane\b": "ln",
    r"\bcourt\b": "ct", r"\bplace\b": "pl", r"\bparkway\b": "pkwy",
    r"\bnorth\b": "n", r"\bsouth\b": "s", r"\beast\b": "e", r"\bwest\b": "w",
}

def slugify_street(text) -> str | None:
    if pd.isna(text):
        return None
    text = str(text).lower().strip()
    for pattern, repl in STREET_REPLACEMENTS.items():
        text = re.sub(pattern, repl, text)
    text = text.replace("?", "")          # drop mangled okina (ʻ) artifacts
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")

df['street_slug'] = df['street_raw'].apply(slugify_street)
cl['street_slug'] = cl['street_raw'].apply(slugify_street)

# sanity check: every master_tmk should now collapse to exactly one street_slug
n_fragmented = (df.groupby('master_tmk')['street_slug'].nunique() > 1).sum()
assert n_fragmented == 0, f"{n_fragmented} buildings still fragmented -- check split_master"

master_buildings = df[['master_tmk', 'street_slug']].drop_duplicates()
listing_buildings = cl[['building_name', 'street_slug']].drop_duplicates()

print(f"Unique master buildings:  {master_buildings['master_tmk'].nunique()}")
print(f"Unique listing buildings: {len(listing_buildings)}")

# ---------- Stage 1 fuzzy match: building street <-> building street ----------
choices = master_buildings['street_slug'].dropna().unique().tolist()

def best_building_match(slug, cutoff=90):
    if not slug:
        return None, 0
    result = process.extractOne(slug, choices, scorer=fuzz.ratio, score_cutoff=cutoff)
    return (result[0], result[1]) if result else (None, 0)

matches = listing_buildings['street_slug'].apply(best_building_match)
listing_buildings = listing_buildings.assign(
    matched_master_slug=[m[0] for m in matches],
    match_score=[m[1] for m in matches],
)

n_matched_buildings = listing_buildings['matched_master_slug'].notna().sum()
print(f"Listing buildings matched to a master building: {n_matched_buildings} / {len(listing_buildings)}")

# ---------- Stage 2: attach real unit numbers within a matched building ----------
# NOTE: several DIFFERENT building_names can share the same street_slug
# (e.g. two listings scraped under different names for the same address), so
# merging `cl` back onto `listing_buildings` on street_slug would duplicate
# rows. Map instead, keyed on the deduplicated street_slug -> match.
street_to_master = (
    listing_buildings[['street_slug', 'matched_master_slug', 'match_score']]
    .drop_duplicates(subset='street_slug')
    .set_index('street_slug')
)
cl['matched_master_slug'] = cl['street_slug'].map(street_to_master['matched_master_slug'])
cl['match_score'] = cl['street_slug'].map(street_to_master['match_score'])

def norm_unit(u):
    if pd.isna(u):
        return None
    return re.sub(r"[^a-z0-9]", "", str(u).lower())

df['unit_norm'] = df['unit_raw'].apply(norm_unit)
cl['unit_norm'] = cl['unit_raw'].apply(norm_unit)

# master lookup keyed by (street_slug, unit_norm) for O(1) exact unit lookups
master_unit_lookup = df.set_index(['street_slug', 'unit_norm'])['parcel_number'].to_dict()

def find_unit_match(row):
    if pd.isna(row['matched_master_slug']) or not row['unit_is_real']:
        return None
    return master_unit_lookup.get((row['matched_master_slug'], row['unit_norm']))

cl['matched_parcel_number'] = cl.apply(find_unit_match, axis=1)

# ---------- Report ----------
print()
print(f"Listing rows landing in a matched building:       {cl['matched_master_slug'].notna().sum()} / {len(cl)}")
print(f"Listing rows with a real (non-junk) unit number:   {cl['unit_is_real'].sum()} / {len(cl)}")
print(f"...of which we found an EXACT master unit match:   {cl['matched_parcel_number'].notna().sum()}")

# ---------- Output: what downstream (qpublic scrape) cell should consume ----------
# One row per matched master unit that's linked to at least one listing,
# ready to feed the parcel-number scrape loop.
scrape_targets = (
    cl.loc[cl['matched_parcel_number'].notna(), ['matched_parcel_number']]
    .rename(columns={'matched_parcel_number': 'parcel_number'})
    .drop_duplicates()
    .merge(df[['parcel_number', 'master_tmk', 'unit_raw', 'modified_address']], on='parcel_number', how='left')
)
print()
print(f"Unique parcels ready for qpublic scrape: {len(scrape_targets)}")
print(scrape_targets.head(18).to_string())
