# WARNING: PLEASE USE TEST-API before production usage or anything else. 
from scrapfly import ScrapflyClient, ScrapeConfig
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import os
import re
import json
import time
import pandas as pd
import csv
import requests
from qpublic_parser import parse_qpublic_html

loaded = load_dotenv(verbose = True)
print(f"Loaded: {loaded}")

api_key = os.getenv("API_TEST") # swap with API_KEY when ready
# client = ScrapflyClient(key = api_key)

SCRAPFLY_SCRAPE_URL = "https://api.scrapfly.io/scrape"

CLIENT_TIMEOUT_SEC = 155 # recommended is 155, but I think I can avoid it...?

PARCEL_REPORT_URL_TEMPLATE = (
    "https://qpublic.schneidercorp.com/Application.aspx"
    "?AppID=1045&LayerID=23342&PageTypeID=4&PageID=9746&Q=727410478&KeyValue={parcel_number}"
)

# does not work because the request is still re-routed to parcel #s
# ADDRESS_REPORT_URL_TEMPLATE = (
#     "https://qpublic.schneidercorp.com/Application.aspx"
#     "https://qpublic.schneidercorp.com/Application.aspx?AppID=1045&LayerID=23342&PageTypeID=4&PageID=9746&Q=676889554&KeyValue=260210260002"
# )


 
def fetch_parcel_html(parcel_number: str, render_js: bool = True) -> str:
    """
    Fetch the raw HTML for a single parcel's qpublic report page via ScrapFly's
    REST API (plain `requests` GET, no SDK).
 
    render_js=True routes the request through ScrapFly's headless-browser
    rendering, which qpublic's ASP.NET postback-heavy UI generally needs (test
    with False first -- it's cheaper and much faster, and only turn this on if
    the parsed fields come back empty).
    """
    url = PARCEL_REPORT_URL_TEMPLATE.format(parcel_number=parcel_number)
    params = {
        "key": api_key,
        "url": url,
        "render_js": str(render_js).lower(),
        "asp": "true",       # anti-scrape-protection bypass; qpublic sits behind some bot defenses
        "country": "us",
    }
    try:
        resp = requests.get(SCRAPFLY_SCRAPE_URL, params=params, timeout=CLIENT_TIMEOUT_SEC)
    except requests.exceptions.ReadTimeout as e:
        # If this still times out even at 155s, the URL template is the more likely
        # culprit -- confirm PARCEL_REPORT_URL_TEMPLATE against a real browser session
        # for this parcel before assuming it's a transient ScrapFly issue.
        raise RuntimeError(
            f"ScrapFly read timed out after {CLIENT_TIMEOUT_SEC}s for parcel {parcel_number}. "
            f"If this persists, verify PARCEL_REPORT_URL_TEMPLATE resolves to a real qpublic "
            f"report page in a browser -- a bad/redirecting URL can make ASP retry until it "
            f"gives up, which looks identical to a network timeout from the client side."
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
 
 
def fetch_and_parse_parcel(parcel_number: str, render_js: bool = True,
                            include_period_detail: bool = False) -> dict:
    html = fetch_parcel_html(parcel_number, render_js=render_js)
    record = parse_qpublic_html(html, include_period_detail=include_period_detail)
    record["_source_parcel_queried"] = parcel_number
    record["_html_length"] = len(html)
    return record
 
 
def fetch_many(parcel_numbers, out_path="parcel_records.jsonl", delay_sec=1.0):
    """
    Batch-fetch and parse a list of parcel numbers, writing one JSON record per
    line to out_path (append mode, so it's resumable -- rerun and skip parcels
    already written).
    """
    already_done = set()
    if os.path.exists(out_path):
        with open(out_path, "r") as f:
            for line in f:
                try:
                    already_done.add(json.loads(line)["_source_parcel_queried"])
                except (json.JSONDecodeError, KeyError):
                    continue
 
    with open(out_path, "a") as out:
        for i, parcel in enumerate(parcel_numbers):
            if parcel in already_done:
                continue
            try:
                record = fetch_and_parse_parcel(parcel)
                out.write(json.dumps(record) + "\n")
                out.flush()
                print(f"[{i+1}/{len(parcel_numbers)}] OK  {parcel}")
            except Exception as e:
                print(f"[{i+1}/{len(parcel_numbers)}] FAIL {parcel}: {e}")
            time.sleep(delay_sec)  # be polite / stay under ScrapFly + qpublic rate limits


def json_to_dataframe(record) -> pd.DataFrame:

    if isinstance(record, dict):
        data = record
    elif hasattr(record, "read"):
        data = json.load(record)
    else:
        with open(record, "r") as f:
            data = json.load(f)
 # 1. Tax status by year   
    df_tax_status = pd.DataFrame(data.get("tax_status_by_year", []))
 
    # 2. Historical tax (excluding the nested payments list, merged in separately)
    df_historical_tax = pd.DataFrame(data.get("historical_tax", []))
    if "payments" in df_historical_tax.columns:
        df_historical_tax = df_historical_tax.drop(columns="payments")
 
    # 3. Merge the two year-indexed tables into one long-form frame
    if not df_tax_status.empty and not df_historical_tax.empty:
        df_year = pd.merge(
            df_tax_status, df_historical_tax,
            on="tax_year", how="outer", suffixes=("_status", "_hist"),
        )
    elif not df_tax_status.empty:
        df_year = df_tax_status.copy()
    elif not df_historical_tax.empty:
        df_year = df_historical_tax.copy()
    else:
        df_year = pd.DataFrame(columns=["tax_year"])
 
    # 4. Payments -> per-year summary columns, merged onto df_year
    payments_rows = []
    for year_entry in data.get("historical_tax", []):
        for payment in year_entry.get("payments", []):
            payments_rows.append({"tax_year": year_entry.get("tax_year"), **payment})
    df_payments = pd.DataFrame(payments_rows)
 
    if not df_payments.empty:
        amount_col = next((c for c in df_payments.columns if "amount" in c.lower()), None)
        date_col = next((c for c in df_payments.columns if "date" in c.lower()), None)
 
        agg_kwargs = {"payment_count": ("tax_year", "size")}
        if amount_col:
            agg_kwargs["total_paid"] = (amount_col, "sum")
        if date_col:
            agg_kwargs["last_payment_date"] = (date_col, "max")
 
        df_payments_summary = (
            df_payments.groupby("tax_year").agg(**agg_kwargs).reset_index()
        )
        df_year = pd.merge(df_year, df_payments_summary, on="tax_year", how="left")
        df_year["payment_count"] = df_year["payment_count"].fillna(0).astype(int)
 
    # 5. Broadcast top-level (non-year) parcel fields onto every row
    top_level_fields = [
        "parcel_number", "location_address", "project_name",
        "legal_information", "current_property_class",
        "_source_parcel_queried", "_html_length",
    ]
    for field in top_level_fields:
        df_year[field] = data.get(field)
 
    # 6. Broadcast dedications notice (no year dimension) onto every row
    ded = data.get("dedications_notice", {})
    df_year["dedications_has_no_data"] = ded.get("dedications_has_no_data")
    no_data_modules = ded.get("no_data_modules")
    df_year["dedications_no_data_modules"] = (
        ", ".join(no_data_modules) if no_data_modules else None
    )
 
    # 7. tax_year first, then the rest, for readability
    if "tax_year" in df_year.columns:
        cols = ["tax_year"] + [c for c in df_year.columns if c != "tax_year"]
        df_year = df_year[cols]
 
    return df_year
 
 
def payments_long_form(record) -> pd.DataFrame:
    """
    Optional companion table: every individual payment as its own row
    (tax_year, payment fields, parcel_number). Use this alongside
    json_to_dataframe() if you need per-payment granularity rather than
    the per-year summary columns it produces.
    """
    if isinstance(record, dict):
        data = record
    elif hasattr(record, "read"):
        data = json.load(record)
    else:
        with open(record, "r") as f:
            data = json.load(f)
 
    rows = []
    for year_entry in data.get("historical_tax", []):
        for payment in year_entry.get("payments", []):
            rows.append({
                "parcel_number": data.get("parcel_number"),
                "tax_year": year_entry.get("tax_year"),
                **payment,
            })
    return pd.DataFrame(rows)
 
 
def records_to_dataframe(records) -> pd.DataFrame:
    """
    Stack json_to_dataframe() output across multiple parcels (e.g. the
    output of fetch_many's parcel_records.jsonl) into one long-form
    DataFrame -- one row per (parcel, tax_year).
    """
    frames = [json_to_dataframe(r) for r in records]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
 
if __name__ == "__main__":
    # Example: one specific unit (matches the sample HTML tested against qpublic_parser.py)
    example_parcel = "260200690004"
    record = fetch_and_parse_parcel(example_parcel)
 
    df = json_to_dataframe(record)
    print(df.head())
    # df.to_csv("parcel_tax_history_long.csv", index=False)