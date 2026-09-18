# WARNING: PLEASE USE TEST-API before production usage or anything else.
"""
qpublic-master.py

Given a Condo Master TMK (e.g. 260210260000), fetch that parcel's own qpublic
report page and pull out the 'Condominium/Apartment Unit Information' grid --
the list of every individual unit's own parcel number (TMK) under that master.

This is the sibling to qpublic-unit.py: that script fetches+parses ONE parcel's
full RPAD detail page; this one fetches+parses a MASTER's page just to harvest
the child parcel numbers, so you can then feed each of those into
qpublic-unit.py's fetch_and_parse_parcel().

Mirrors qpublic-unit.py's structure/conventions on purpose so the two are easy
to read side by side.
"""
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import os
import json
import time
import requests
import pandas as pd

from qpublic_parser import parse_condo_units, is_condo_master, parse_parcel_info

loaded = load_dotenv(verbose=True)
print(f"Loaded: {loaded}")

api_key = os.getenv("API_TEST")  # swap with API_KEY when ready

SCRAPFLY_SCRAPE_URL = "https://api.scrapfly.io/scrape"
CLIENT_TIMEOUT_SEC = 155
# A Condo Master's report page is fetched through the exact same endpoint as any
# other parcel -- reuse qpublic-unit.py's proven AppID/LayerID/PageTypeID/PageID/Q
# template verbatim, just with the master TMK as KeyValue. (An earlier version of
# this script tried the "clean" /HonoluluCountyHI/Parcels/Report/Report?KeyValue=
# URL pulled from the gtag analytics call in the saved sample HTML -- that's the
# page's own canonical URL for tracking purposes, not a URL you can navigate to
# directly without existing session/redirect state, and it 404s on a fresh fetch.)
MASTER_REPORT_URL_TEMPLATE = (
    "https://qpublic.schneidercorp.com/Application.aspx"
    "?AppID=1045&LayerID=23342&PageTypeID=4&PageID=9746&Q=727410478&KeyValue={master_tmk}"
)


def fetch_master_html(master_tmk: str, render_js: bool = True) -> str:
    """
    Fetch the raw HTML for a Condo Master's own report page via ScrapFly's
    REST API (plain `requests` GET, no SDK) -- same approach as
    qpublic-unit.py's fetch_parcel_html.

    render_js=True routes through ScrapFly's headless-browser rendering.
    qpublic sits behind a Cloudflare Turnstile challenge (visible in the page's
    <script src="https://challenges.cloudflare.com/turnstile/...">), so this
    generally needs to stay True -- try False first only if you want to save
    credits and confirm it still works for your account/plan.
    """
    url = MASTER_REPORT_URL_TEMPLATE.format(master_tmk=master_tmk)
    params = {
        "key": api_key,
        "url": url,
        "render_js": str(render_js).lower(),
        "asp": "true",       # anti-scrape-protection bypass; qpublic sits behind Cloudflare
        "country": "us",
    }
    try:
        resp = requests.get(SCRAPFLY_SCRAPE_URL, params=params, timeout=CLIENT_TIMEOUT_SEC)
    except requests.exceptions.ReadTimeout as e:
        raise RuntimeError(
            f"ScrapFly read timed out after {CLIENT_TIMEOUT_SEC}s for master TMK {master_tmk}. "
            f"If this persists, verify MASTER_REPORT_URL_TEMPLATE resolves to a real qpublic "
            f"report page in a browser for this TMK before assuming it's transient."
        ) from e

    resp.raise_for_status()
    data = resp.json()

    result = data.get("result", {})
    status_code = result.get("status_code")
    if status_code and status_code >= 400:
        raise RuntimeError(f"Upstream qpublic page returned status {status_code} for {url}")

    html = result.get("content")
    if not html:
        raise RuntimeError(f"No content returned by ScrapFly for {url}: {json.dumps(data)[:500]}")
    return html


def fetch_and_parse_master(master_tmk: str, render_js: bool = True) -> dict:
    """
    Returns {'master_tmk', 'base_address', 'is_condo_master', 'unit_count', 'units'}.
    Each dict in 'units' is {'parcel_number', 'unit_number', 'modified_address'} --
    parcel_number is each individual unit's own TMK, and modified_address is the
    master's own Location Address with " APT {unit_number}" appended (matches the
    "address" column convention already used in master_tmk_address.csv, e.g.
    "445 SEASIDE AVE APT 4214").
    """
    html = fetch_master_html(master_tmk, render_js=render_js)
    soup = BeautifulSoup(html, 'lxml')
    units = parse_condo_units(soup)

    base_address = parse_parcel_info(soup).get('Location Address')
    if base_address:
        base_address = ' '.join(base_address.split())  # collapse "445   SEASIDE AVE" -> "445 SEASIDE AVE"

    for u in units:
        u['modified_address'] = (
            f"{base_address} APT {u['unit_number']}"
            if base_address and u.get('unit_number') else None
        )

    return {
        "master_tmk": master_tmk,
        "base_address": base_address,
        "is_condo_master": is_condo_master(soup),
        "unit_count": len(units),
        "units": units,
        "_html_length": len(html),
    }


def units_to_dataframe(records) -> pd.DataFrame:
    """
    Flattens an iterable of records (as returned by fetch_and_parse_master, or
    loaded from a checkpoint file) -- one record per MASTER with a nested
    'units' list -- into one row per individual unit/parcel.

    Output columns: master_tmk, parcel_number, unit_number, modified_address
    """
    flat_rows = []
    for rec in records:
        master_tmk = rec["master_tmk"]
        for u in rec.get("units", []):
            flat_rows.append({
                "master_tmk": master_tmk,
                "parcel_number": u["parcel_number"],
                "unit_number": u["unit_number"],
                "modified_address": u.get("modified_address"),
            })
    return pd.DataFrame(flat_rows, columns=["master_tmk", "parcel_number", "unit_number", "modified_address"])


def fetch_many_masters(master_tmks, out_path="master_units.jsonl", delay_sec=1.0) -> pd.DataFrame:
    """
    Batch-fetch and parse a list of master TMKs.

    Writes one JSON record per line to out_path as it goes (append mode) purely
    as a resumable checkpoint -- rerun and it skips masters already fetched
    instead of re-spending ScrapFly credits. You don't need to touch that file
    yourself: this function returns the combined result as a DataFrame directly
    (every unit, across every master TMK fetched so far, including ones loaded
    from a prior run's checkpoint).
    """
    records = {}
    if os.path.exists(out_path):
        with open(out_path, "r") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    records[rec["master_tmk"]] = rec
                except (json.JSONDecodeError, KeyError):
                    continue

    with open(out_path, "a") as out:
        for i, master_tmk in enumerate(master_tmks):
            if master_tmk in records:
                print(f"[{i+1}/{len(master_tmks)}] SKIP {master_tmk} (already done)")
                continue
            try:
                record = fetch_and_parse_master(master_tmk)
                out.write(json.dumps(record) + "\n")
                out.flush()
                records[master_tmk] = record
                print(f"[{i+1}/{len(master_tmks)}] OK  {master_tmk}  ({record['unit_count']} units)")
            except Exception as e:
                print(f"[{i+1}/{len(master_tmks)}] FAIL {master_tmk}: {e}")
            time.sleep(delay_sec)  # be polite / stay under ScrapFly + qpublic rate limits

    return units_to_dataframe(records.values())


def load_checkpoint_dataframe(jsonl_path="master_units.jsonl") -> pd.DataFrame:
    """Reload a DataFrame from a checkpoint file without hitting the network again."""
    records = []
    with open(jsonl_path, "r") as f:
        for line in f:
            records.append(json.loads(line))
    return units_to_dataframe(records)


def get_master_tmks_from_csv(ods_path="data/manual-tmk-data.ods", column="CONDO MASTER TMK",
                              sheet_name="README"):
    """
    Unique master TMKs from the manual TMK workbook, as strings.

    Reads sheet_name from ods_path (requires the 'odfpy' package -- pip install
    odfpy). TMK columns loaded from Excel/ODS often come back as int64 (clean)
    or float64 (if the column has any blank cells, e.g. 260210260000.0) --
    both get normalized to plain digit strings here so they match the string
    KeyValue qpublic's URLs expect.
    """
    try:
        df = pd.read_excel(ods_path, engine="odf", sheet_name=sheet_name)
    except Exception as e:
        print(f"Error reading {ods_path!r} sheet {sheet_name!r}: {e}")
        return []

    if column not in df.columns:
        print(f"Error: column {column!r} not found in {ods_path!r}. Columns present: {list(df.columns)}")
        return []

    master_tmk = df[column].dropna()
    if master_tmk.dtype == "int64":
        master_tmk = master_tmk.astype(str)
    elif master_tmk.dtype == "float64":
        master_tmk = master_tmk.astype("int64").astype(str)  # drop the trailing ".0"
    else:
        master_tmk = master_tmk.astype(str).str.strip()

    return sorted(master_tmk.unique().tolist())


if __name__ == "__main__":
    # Example: one specific master (matches the sample HTML this script was built/tested against)
    example_master = "260210260000"
    record = fetch_and_parse_master(example_master)
    print(f"master_tmk={record['master_tmk']}  base_address={record['base_address']}  "
          f"is_condo_master={record['is_condo_master']}  unit_count={record['unit_count']}")
    print(json.dumps(record["units"][:3], indent=2))

    df = units_to_dataframe([record])
    print(df.head())

    # Full batch run over every master TMK already in the CSV -- returns a DataFrame
    # directly (master_tmk, parcel_number, unit_number, modified_address), while
    # checkpointing progress to master_units.jsonl as it goes:
    # masters = get_master_tmks_from_csv()
    # df = fetch_many_masters(masters, out_path="master_units.jsonl")
    # df.to_parquet("master_units.parquet", index=False)  # optional, if you still want a file