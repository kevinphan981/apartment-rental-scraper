# WARNING: PLEASE USE TEST-API before production usage or anything else. 
from scrapfly import ScrapflyClient, ScrapeConfig
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import os
import re
import json
import time
import csv
import requests
from qpublic_parser import parse_qpublic_html

loaded = load_dotenv(verbose = True)
print(f"Loaded: {loaded}")

api_key = os.getenv("API_TEST") # swap with API_KEY when ready
# client = ScrapflyClient(key = api_key)

SCRAPFLY_SCRAPE_URL = "https://api.scrapfly.io/scrape"
CLIENT_TIMEOUT_SEC = 155
PARCEL_REPORT_URL_TEMPLATE = (
    "https://qpublic.schneidercorp.com/Application.aspx"
    "?AppID=1045&LayerID=23342&PageTypeID=4&PageID=9746&Q=727410478&KeyValue={parcel_number}"
)


 
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
 
if __name__ == "__main__":
    # Example: one specific unit (matches the sample HTML tested against qpublic_parser.py)
    example_parcel = "260200690004"
    record = fetch_and_parse_parcel(example_parcel)
    print(json.dumps(record, indent=2))