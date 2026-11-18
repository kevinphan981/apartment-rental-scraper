# WARNING: PLEASE USE TEST-API before production usage or anything else.
from scrapfly import ScrapflyClient, ScrapeConfig
import time
import json
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import os
import re
import csv

loaded = load_dotenv(verbose=True)
print(f"Loaded: {loaded}")
api_key = os.getenv("API_TEST")  # swap with API_KEY when ready

# ----------------- URL PREP ----------------------

# TODO: make a method that changes the URL based on my master CSV


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)  # drop punctuation
    text = re.sub(r"\s+", "-", text)  # spaces to hyphens
    return text


def build_apartments_url(street_address: str, city: str, state: str) -> str:
    """
    Query using the apartment (general or specific) with the city and state
    this is how the url is built:
    """
    street_slug = slugify(street_address)
    city_state_slug = f"{slugify(city)}-{slugify(state)}"

    return f"https://www.apartments.com/{street_slug}_{city_state_slug}/"


def build_county_url(county: str, state: str) -> str:
    """
    We query using the general location (county in this case) and then build the url like that.
    """

    county_slug = slugify(county)
    state_slug = slugify(state)

    return f"https://www.apartments.com/{county_slug}-county-{state_slug}/"


# ------------------- Scrapify Scrape --------------
# it works, now we have to fetch the real information...
# URL = "https://www.apartments.com/445-seaside-ave_honolulu-hi/?rid=6jtjznv"


def build_full_address(street_address: str, unit_number: str) -> str:
    """
    Combine the base street address with the unit number into one mailing
    address, without double-appending the unit if apartments.com already
    baked it into street_address (e.g. "445 Seaside Ave Unit 1508").
    """
    street_address = (street_address or "").strip()
    unit_number = (unit_number or "").strip()

    if not unit_number:
        return street_address

    if re.search(
        rf"\bunit\s*#?\s*{re.escape(unit_number)}\b", street_address, re.IGNORECASE
    ):
        return street_address

    return f"{street_address} Unit {unit_number}"


BASE_URL = "https://www.apartments.com"


def is_placeholder_unit(unit_number: str) -> bool:
    """
    Building-profile pages sometimes include 'SI FLn-IDxxxxxxxP' style rows --
    floorplan placeholders not yet tied to a real unit, often carrying a
    nightly/hotel-style rate synced into the monthly rent field rather than
    an actual rentable unit. Flag them so they're easy to filter out.
    """
    return bool(re.match(r"^SI\s+FL\d+-ID", unit_number or "", re.IGNORECASE))


def _clean_bed_bath_sqft(el) -> str:
    """Join a bed/bath/sqft block's text nodes, dropping the '|' separators."""
    if el is None:
        return ""
    return " | ".join(s.strip() for s in el.stripped_strings if s.strip() != "|")


def parse_building_page(soup, url: str, accessed_at: str) -> list[dict] | None:
    """
    Parse the "building profile" template (canonical /building/{slug}/{id}/),
    which apartments.com serves instead of a search-results page when the
    address matches a known multi-unit community. Returns None if this page
    is not a building-profile page, so the caller can fall back to the
    search-results parser.
    """
    units_section = soup.select_one("section.building-profile-units")
    if units_section is None:
        return None

    building_name_el = soup.select_one("h1.heading")
    building_name = building_name_el.get_text(strip=True) if building_name_el else ""

    address_el = soup.select_one("p.building-address")
    building_address = " ".join(address_el.stripped_strings) if address_el else ""

    available_lbl = soup.select_one("p.available-units-lbl")
    available_units_text = available_lbl.get_text(strip=True) if available_lbl else ""

    canonical_el = soup.select_one("link[rel='canonical']")
    canonical_url = canonical_el.get("href", "") if canonical_el else ""

    listings = []
    for card in units_section.select("a.unit-card"):
        href = card.get("href", "")
        listing_url = href if href.startswith("http") else f"{BASE_URL}{href}"

        unit_number_el = card.select_one(".unit-number")
        unit_number = unit_number_el.get_text(strip=True) if unit_number_el else ""

        available_date_el = card.select_one(".available-date")
        available_date = (
            available_date_el.get_text(strip=True) if available_date_el else ""
        )

        rent_el = card.select_one(".rent")
        # the rent text is a bare text node sitting before the nested
        # "Total Monthly Price" span, so grab only that direct text node
        rent = ""
        if rent_el is not None:
            first_text = rent_el.find(string=True, recursive=False)
            rent = first_text.strip() if first_text else rent_el.get_text(strip=True)

        desc_paragraphs = card.select(".description p")
        lease_term = ""
        bed_bath_sqft = ""
        if len(desc_paragraphs) == 2:
            lease_term = desc_paragraphs[0].get_text(strip=True)
            bed_bath_sqft = _clean_bed_bath_sqft(desc_paragraphs[1])
        elif len(desc_paragraphs) == 1:
            bed_bath_sqft = _clean_bed_bath_sqft(desc_paragraphs[0])

        listings.append(
            {
                "listing_id": listing_url.rstrip("/").rsplit("/", 1)[-1],
                "page_type": "building_profile",
                "building_name": building_name,
                "address": build_full_address(building_address, unit_number),
                "unit_number": unit_number,
                "is_placeholder": is_placeholder_unit(unit_number),
                "rent": rent,
                "bed_bath_sqft": bed_bath_sqft,
                "lease_term": lease_term,
                "available_date": available_date,
                "listing_url": listing_url,
                "source_page_url": url,
                "canonical_url": canonical_url,
                "building_available_units_label": available_units_text,
                "date_accessed": accessed_at,
            }
        )

    return listings


# we have a problem, where this search_page, and the other page that I have are completely DIFFERENT, so we're going to have to parse it in another method
# this is meant for apartments NEAR BY a certain address... basically the default address search schema we have but with the additional building-unit placard
def parse_search_page(soup, url: str, accessed_at: str) -> list[dict]:
    """Parse the generic 'near address' search-results template (article.placard)."""
    listings = []

    for article in soup.select("article.placard"):
        listing_id = article.get("data-listingid", "")

        # i don't quite understand it myself, but the search page is now different than the last time i actually went through this stuff

        # should compare the txt files to see what I was fetching
        street_address = article.get("data-streetaddress", "")
        unit_number = article.get("data-unitnumber", "")
        listing_url = article.get("data-url", "")

        title_el = article.select_one(".property-title, .js-placardTitle")
        full_title = title_el.get_text(strip=True) if title_el else ""

        # we have two different types of placard layouts for single unit + multi unit (the default building view)
        bed_rent_boxes = article.select(".bedRentBox")

        if bed_rent_boxes:
            for box in bed_rent_boxes:
                bed_text_el = box.select_one(".bedTextBox")
                price_el = box.select_one(".priceTextBox")
                listings.append(
                    {
                        "listing_id": listing_id,
                        "page_type": "search_results",
                        "building_name": full_title,
                        "address": build_full_address(street_address, unit_number),
                        "unit_number": unit_number,
                        "is_placeholder": is_placeholder_unit(unit_number),
                        "rent": price_el.get_text(strip=True) if price_el else "",
                        "bed_bath_sqft": bed_text_el.get_text(strip=True)
                        if bed_text_el
                        else "",
                        "lease_term": "",
                        "available_date": "",
                        "listing_url": listing_url,
                        "source_page_url": url,
                        "canonical_url": "",
                        "building_available_units_label": "",
                        "date_accessed": accessed_at,
                    }
                )
        else:
            price_el = article.select_one(".property_pricing")
            bed_range_el = article.select_one(".bed-range")
            bed_bath = _clean_bed_bath_sqft(bed_range_el)

            listings.append(
                {
                    "listing_id": listing_id,
                    "page_type": "search_results",
                    "building_name": full_title,
                    "address": build_full_address(street_address, unit_number),
                    "unit_number": unit_number,
                    "is_placeholder": is_placeholder_unit(unit_number),
                    "rent": price_el.get_text(strip=True) if price_el else "",
                    "bed_bath_sqft": bed_bath,
                    "lease_term": "",
                    "available_date": "",
                    "listing_url": listing_url,
                    "source_page_url": url,
                    "canonical_url": "",
                    "building_available_units_label": "",
                    "date_accessed": accessed_at,
                }
            )
    return listings


# Fixed CSV header shared by every parser. Rows from the older parsers simply
# leave the numeric columns blank.
FIELDNAMES = [
    "listing_id",
    "page_type",
    "record_type",
    "building_name",
    "address",
    "unit_number",
    "is_placeholder",
    "bedrooms",
    "bathrooms",
    "bath_range",
    "sqft",
    "rent",
    "rent_numeric",
    "rent_is_starting_at",
    "bed_bath_sqft",
    "lease_term",
    "available_date",
    "listing_url",
    "building_profile_url",
    "source_page_url",
    "canonical_url",
    "building_available_units_label",
    "date_accessed",
]


def is_county_page(soup) -> bool:
    """County pages use property-level placards with no per-unit data-* attrs."""
    placards = soup.select("article.placard")
    if not placards:
        return False
    return not any(p.get("data-url") for p in placards)


# ----------------- small text parsers -----------------


def _parse_bedrooms(text: str):
    """'Studio' -> 0, '2 Beds' -> 2, '3 Beds+' -> 3, '1-2 Beds' -> None (ambiguous)."""
    text = text or ""
    if re.match(r"^\s*studio", text, re.IGNORECASE):
        return 0
    m = re.match(r"^\s*(\d+)\s*beds?\b", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_baths(text: str):
    m = re.search(r"([\d.]+)\s*baths?\b", text or "", re.IGNORECASE)
    return float(m.group(1)) if m else None


def _parse_sqft(text: str):
    m = re.search(r"([\d,]+)\s*sq\s*ft", text or "", re.IGNORECASE)
    return int(m.group(1).replace(",", "")) if m else None


def _parse_rent(text: str):
    """
    '$3,534+' -> (3534, True)   the '+' means "starting at", not a fixed price
    '$1,913'  -> (1913, False)
    'Call for Rent' -> (None, False)
    """
    m = re.search(r"\$\s*([\d,]+)", text or "")
    if not m:
        return None, False
    return int(m.group(1).replace(",", "")), "+" in text


def _parse_share_lines(btn) -> dict:
    """
    The kebab button's JSON, e.g.
      [url, "", "2570 Dole St", "Honolulu, HI 96822", "", "Studio - 4 bd",
       "1 - 2 ba", "Total Monthly Price: $1,605 - $6,420", "11 Month Lease"]
    Lines are identified by pattern because some templates omit some of them.
    """
    out = {"beds": "", "baths": "", "rent": "", "lease_term": ""}
    if btn is None:
        return out
    try:
        lines = json.loads(btn.get("data-listing-share-lines", "[]"))
    except json.JSONDecodeError:
        return out

    for line in lines[1:]:
        line = (line or "").strip()
        if not line:
            continue
        if "$" in line:
            out["rent"] = line.replace("Total Monthly Price:", "").strip()
        elif re.search(r"month lease", line, re.IGNORECASE):
            out["lease_term"] = line
        elif re.search(r"\bba\b|\bbath", line, re.IGNORECASE):
            out["baths"] = line
        elif re.search(r"\bbd\b|\bbed|studio", line, re.IGNORECASE):
            out["beds"] = line
    return out


# ----------------- the parser -----------------


def parse_county_page(soup, url: str, accessed_at: str) -> list[dict]:
    """
    Parse the county / area search template.

    Returns ONE ROW PER BEDROOM/PRICE BAND, so a building listing
    "Studio: $2,251; 2 Beds: $1,913+" becomes two rows. Single-unit
    placards (featured / for-rent-by-owner) become one row each.

    record_type:
      property        a building band (rent_numeric is usually a "starting at"
                      price -- see rent_is_starting_at)
      featured_unit   promoted single unit that shares its listing_id with a
                      property placard
      fbo_unit        for-rent-by-owner single unit

    bathrooms is filled only when the placard pins it down (single units, or
    a property whose whole bath range is one value); otherwise it is blank and
    bath_range carries the property-wide range, e.g. "1 - 2".
    """
    listings = []

    for article in soup.select("article.placard"):
        classes = article.get("class", [])
        btn = article.select_one("button.listing-more-btn")

        # identity: kebab button first (every template has it), DOM fallback
        if btn is not None:
            name = btn.get("data-listing-name", "").strip()
            address = btn.get("data-listing-address", "").strip()
            listing_url = btn.get("data-listing-url", "").strip()
        else:
            name = address = listing_url = ""

        if not name:
            t = article.select_one(".property-title, .js-placardTitle")
            name = t.get_text(strip=True) if t else ""
        if not address:
            a = article.select_one(".property-address")
            address = a.get("title", "") or (a.get_text(strip=True) if a else "")

        # silver featured units carry a '#xxxx-unit/' fragment that tells them
        # apart from their parent property; the kebab URL drops it
        link = article.select_one("a.property-link[href]")
        href = link["href"] if link else ""
        if href and "#" in href:
            listing_url = href
        if not listing_url:
            listing_url = href
        if listing_url and not listing_url.startswith("http"):
            listing_url = f"{BASE_URL}{listing_url}"

        share = _parse_share_lines(btn)

        # classify
        if "tmpl-premiumplus" in classes or article.select_one(
            ".for-rent-by-owner-logo-wrapper"
        ):
            record_type = "fbo_unit"
        elif "featured-unit" in classes:
            record_type = "featured_unit"
        else:
            record_type = "property"

        lease_term = share["lease_term"]
        if not lease_term:
            lt = article.select_one(".lease-term-month")
            lease_term = lt.get_text(strip=True) if lt else ""

        # fbo units sit inside a building: "More Units at Foster Tower"
        building_name = name
        building_profile_url = ""
        bld = article.select_one("a.js-building-propertypurl")
        if bld is not None:
            building_name = bld.get_text(strip=True) or name
            bhref = bld.get("href", "")
            building_profile_url = (
                bhref if bhref.startswith("http") else f"{BASE_URL}{bhref}"
            )

        unit_match = re.search(r"\bunit\s*#?\s*([A-Za-z0-9-]+)", address, re.IGNORECASE)
        unit_number = unit_match.group(1) if unit_match else ""

        base = {
            "listing_id": article.get("data-listingid", ""),
            "page_type": "county_search",
            "record_type": record_type,
            "building_name": building_name,
            "address": address,
            "unit_number": unit_number,
            "is_placeholder": is_placeholder_unit(unit_number),
            "lease_term": lease_term,
            "available_date": "",
            "listing_url": listing_url,
            "building_profile_url": building_profile_url,
            "source_page_url": url,
            "canonical_url": "",
            "building_available_units_label": "",
            "date_accessed": accessed_at,
        }

        # property-wide bath info, used to fill bathrooms when unambiguous
        exact_bath = re.fullmatch(
            r"\s*([\d.]+)\s*ba\s*", share["baths"] or "", re.IGNORECASE
        )
        property_baths = float(exact_bath.group(1)) if exact_bath else None
        bath_range = (
            "" if exact_bath else re.sub(r"\s*ba\s*$", "", share["baths"] or "").strip()
        )

        def make_row(bed_text, price_text, bath_text="", from_unit_text=False):
            if from_unit_text:
                # "2 Beds, 2 Baths, 937 sq ft" -- everything is in one string
                baths = _parse_baths(bed_text)
                sqft = _parse_sqft(bed_text)
                descr = bed_text
                br = ""
            else:
                baths = property_baths
                sqft = None
                br = bath_range
                descr = " | ".join(x for x in (bed_text, share["baths"]) if x)
            rent_num, starting = _parse_rent(price_text)
            return {
                **base,
                "bedrooms": _parse_bedrooms(bed_text),
                "bathrooms": baths,
                "bath_range": br,
                "sqft": sqft,
                "rent": price_text,
                "rent_numeric": rent_num,
                "rent_is_starting_at": starting,
                "bed_bath_sqft": descr,
            }

        boxes = article.select(".bedRentBox")
        if boxes:
            for box in boxes:
                bed_el = box.select_one(".bedTextBox")
                price_el = box.select_one(".priceTextBox")
                bed_txt = bed_el.get_text(strip=True) if bed_el else ""
                price_txt = price_el.get_text(strip=True) if price_el else ""
                listings.append(
                    make_row(
                        bed_txt, price_txt, from_unit_text=(record_type != "property")
                    )
                )
        else:
            # for-rent-by-owner: price in .price-range, details in .property-beds
            price_el = article.select_one(".price-range, .property_pricing")
            price_txt = price_el.get_text(strip=True) if price_el else share["rent"]
            detail = article.select_one(".property-beds")
            if detail is not None:
                listings.append(
                    make_row(
                        detail.get_text(strip=True), price_txt, from_unit_text=True
                    )
                )
            else:
                listings.append(make_row(share["beds"], price_txt))

    return listings


def get_county_max_page(soup) -> int:
    """Largest page number in the paging bar (its links use data-page, not href)."""
    pages = []
    for el in soup.select("#paging [data-page]"):
        try:
            pages.append(int(el["data-page"]))
        except (KeyError, ValueError):
            pass
    return max(pages) if pages else 1


def scrape_county(
    county: str,
    state: str,
    api_key: str,
    max_pages: int | None = None,
    delay: float = 1.0,
    render_js: bool = True,
) -> list[dict]:
    """
    Crawl every results page for a county and return one row per
    bedroom/price band (see parse_county_page).

    Page N is fetched from {county_url}{N}/ . max_pages caps the crawl for
    testing; render_js=False is worth trying to save credits.
    """
    scrapfly = ScrapflyClient(key=api_key)
    base_url = build_county_url(county, state)
    accessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def fetch_soup(target_url: str):
        result = scrapfly.scrape(
            ScrapeConfig(
                url=target_url,
                asp=True,
                render_js=render_js,
                country="us",
            )
        )
        return BeautifulSoup(result.content, "lxml")

    soup = fetch_soup(base_url)
    total_pages = get_county_max_page(soup)
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)
    print(f"  -> county search: crawling {total_pages} page(s) from {base_url}")

    all_rows = parse_county_page(soup, base_url, accessed_at)
    print(f"  -> page 1: {len(all_rows)} rows")

    for page in range(2, total_pages + 1):
        time.sleep(delay)
        page_url = f"{base_url}{page}/"
        try:
            page_soup = fetch_soup(page_url)
        except Exception as e:
            print(f"  -> page {page} failed: {e}")
            continue
        rows = parse_county_page(page_soup, page_url, accessed_at)
        print(f"  -> page {page}: {len(rows)} rows")
        if not rows:
            break  # ran off the end (or got blocked) -- stop rather than burn credits
        all_rows.extend(rows)

    # de-dupe on (url, band): the url alone is no longer unique now that a
    # building produces several rows
    seen, unique = set(), []
    for row in all_rows:
        key = (row["listing_url"] or row["listing_id"], row["bed_bath_sqft"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def find_building_profile_url(soup, base_url: str = BASE_URL) -> str | None:
    """
    Look for a link to the master building-profile page. This shows up on
    intermediate "specific PMC/property" pages (e.g. .../1777-ala-moana-blvd-
    honolulu-hi/xfmtm4m/) as "Learn more about {Building Name}", but never on
    the generic underscore 'near address' search page itself. Matching on the
    href substring rather than a specific CSS class means this keeps working
    even if that intermediate template's markup differs by PMC/module.
    """
    a = soup.select_one('a[href*="/building/"]')
    if a is None:
        return None
    href = a.get("href", "")
    return href if href.startswith("http") else f"{base_url}{href}"


def scrape_listings(url: str, api_key: str, max_probe_pages: int = 3) -> list[dict]:
    """
    Fetch a single apartments.com URL and parse it, regardless of which
    template apartments.com decides to serve back, then try to find the
    fuller building-profile roster if one exists:

      1. Fetch `url`. If it's already a building-profile page, parse and
         return -- done in one request.
      2. Otherwise parse it as a generic search-results page (article.placard).
      3. Look for a "/building/..." link on that page. It's usually not
         there yet -- it lives one hop deeper, on the individual PMC/property
         pages that each placard's listing_url points to. So fetch up to
         `max_probe_pages` of those (stopping at the first hit) and look
         there instead.
      4. If a building link turns up, fetch it and merge its units in
         (deduped by unit_number), since it's the more complete source.

    This is link-following only -- no site search box, no typeahead, no
    building-name guessing required.
    """
    scrapfly = ScrapflyClient(key=api_key)

    def fetch_soup(target_url: str):
        result = scrapfly.scrape(
            ScrapeConfig(
                url=target_url,
                asp=True,
                render_js=True,
                country="us",
            )
        )

        print(
            result.context.get("url"), len(result.content)
        )  # final URL after redirects
        with open("debug_last.html", "w", encoding="utf-8") as f:
            f.write(result.content)
        return BeautifulSoup(result.content, "lxml")

    accessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    soup = fetch_soup(url)

    building_listings = parse_building_page(soup, url, accessed_at)
    if building_listings is not None:
        print(
            f"  -> resolved directly to a building profile page ({len(building_listings)} units)"
        )
        return building_listings

    search_listings = parse_search_page(soup, url, accessed_at)
    print(f"  -> resolved to a search-results page ({len(search_listings)} listings)")

    building_url = find_building_profile_url(soup)

    if building_url is None:
        candidate_urls = list(
            dict.fromkeys(
                row["listing_url"] for row in search_listings if row["listing_url"]
            )
        )[:max_probe_pages]

        for candidate_url in candidate_urls:
            try:
                candidate_soup = fetch_soup(candidate_url)
            except Exception:
                continue
            building_url = find_building_profile_url(candidate_soup)
            if building_url:
                print(f"  -> found a building-profile link on {candidate_url}")
                break

    if building_url is None:
        return search_listings

    building_soup = fetch_soup(building_url)
    fuller_listings = (
        parse_building_page(building_soup, building_url, accessed_at) or []
    )
    print(f"  -> building profile at {building_url} added {len(fuller_listings)} units")

    # merge, de-duped by unit number; the building-profile copy wins when a
    # unit shows up in both (it carries lease_term/available_date the
    # search-results template doesn't)
    merged = {row["unit_number"]: row for row in search_listings if row["unit_number"]}
    merged.update(
        {row["unit_number"]: row for row in fuller_listings if row["unit_number"]}
    )
    return list(merged.values())


FIELDNAMES = [
    "listing_id",
    "unit_id",
    "page_type",
    "record_type",
    "building_name",
    "address",
    "unit_number",
    "model_name",
    "lease_type",
    "is_placeholder",
    "bedrooms",
    "bathrooms",
    "bath_range",
    "sqft",
    "rent",
    "rent_numeric",
    "rent_is_starting_at",
    "bed_bath_sqft",
    "lease_term",
    "available_date",
    "listing_url",
    "building_profile_url",
    "source_page_url",
    "canonical_url",
    "building_available_units_label",
    "date_accessed",
]

# On per-bedroom-lease (student-style) buildings, data-unit holds the lease
# type instead of a unit number.
LEASE_TYPE_LABELS = {"private", "entire", "shared"}


def parse_property_page(soup, url: str, accessed_at: str) -> list[dict] | None:
    """
    Parse an individual property page (the /{slug}/{id}/ template with a
    #pricingView grid). Returns None when the page has no pricing grid, so the
    caller can keep the county band rows instead; returns [] when the grid
    exists but nothing is available.

    Each unit appears once per tab (the "all" tab plus its bedroom tab), so
    rows are de-duplicated on data-unitkey.
    """
    if soup.select_one("#pricingView") is None:
        return None

    name_el = soup.select_one("h1")
    building_name = name_el.get_text(strip=True) if name_el else ""

    addr_el = soup.select_one(".propertyAddressContainer")
    address = ""
    if addr_el is not None:
        address = re.sub(r"\s+,", ",", " ".join(addr_el.stripped_strings)).strip()

    canonical_el = soup.select_one("link[rel='canonical']")
    canonical_url = canonical_el.get("href", "") if canonical_el else url

    avail_el = soup.select_one(".availability")
    available_label = avail_el.get_text(strip=True) if avail_el else ""

    rows, seen = [], set()
    for li in soup.select("#pricingView li.unitContainer"):
        unit_id = li.get("data-unitkey") or li.get("data-rentalkey") or ""
        if not unit_id or unit_id in seen:
            continue
        seen.add(unit_id)

        def cell(cls):
            el = li.select_one(f".{cls}")
            if el is None:
                return ""
            # drop the screen-reader-only labels ("price", "availibility", ...)
            return " ".join(
                s
                for s in el.stripped_strings
                if s.lower()
                not in {"price", "square feet", "availibility", "availability", "unit"}
            )

        label = (li.get("data-unit") or "").strip()
        if label.lower() in LEASE_TYPE_LABELS:
            lease_type, unit_number = label, ""
        else:
            lease_type, unit_number = "", label

        beds_raw, baths_raw = li.get("data-beds", ""), li.get("data-baths", "")
        bedrooms = int(beds_raw) if beds_raw.isdigit() else None
        try:
            bathrooms = float(baths_raw)
        except ValueError:
            bathrooms = None

        sqft_digits = re.sub(r"[^\d]", "", cell("sqftColumn"))
        sqft = int(sqft_digits) if sqft_digits else None

        price_text = cell("pricingColumn")
        rent_numeric = None
        if li.get("data-maxrent", "").isdigit():
            rent_numeric = int(li["data-maxrent"])
        else:
            rent_numeric, _ = _parse_rent(price_text)

        # "Studio • 1 Bath • 263 Sq Ft • Available Now" -> drop the availability tail
        specs = re.sub(r"\s*•\s*Available.*$", "", li.get("data-applynow-specs", ""))
        specs = specs.replace(" • ", " | ")

        rows.append(
            {
                "listing_id": unit_id,
                "unit_id": unit_id,
                "page_type": "property_page",
                "record_type": "unit",
                "building_name": building_name,
                "address": address,
                "unit_number": unit_number,
                "model_name": li.get("data-model", ""),
                "lease_type": lease_type,
                "is_placeholder": is_placeholder_unit(unit_number),
                "bedrooms": bedrooms,
                "bathrooms": bathrooms,
                "bath_range": "",
                "sqft": sqft,
                "rent": price_text,
                "rent_numeric": rent_numeric,
                "rent_is_starting_at": False,  # unit rows carry the unit's own price
                "bed_bath_sqft": specs,
                "lease_term": "",
                "available_date": cell("availableColumn"),
                # same shape as the county page's featured-unit links; inferred from
                # one example ("#sy241h5-2-unit/", 2 = bedrooms)
                "listing_url": f"{canonical_url}#{unit_id}-{bedrooms}-unit/"
                if bedrooms is not None
                else canonical_url,
                "building_profile_url": canonical_url,
                "source_page_url": url,
                "canonical_url": canonical_url,
                "building_available_units_label": available_label,
                "date_accessed": accessed_at,
            }
        )
    return rows


def _unit_key_from_url(listing_url: str) -> str:
    """'.../exk42hm/#sy241h5-2-unit/' -> 'sy241h5' (empty string if no fragment)."""
    m = re.search(r"#([a-z0-9]+)-\d+-unit", listing_url or "")
    return m.group(1) if m else ""


def enrich_with_building_units(
    county_rows: list[dict],
    fetch_soup,
    max_buildings: int | None = None,
    delay: float = 1.0,
) -> list[dict]:
    """
    Stage two. For every building that appeared as band rows
    (record_type == "property") fetch its property page once and replace its
    band rows with real unit rows.

      - buildings whose page fails or has no pricing grid KEEP their band rows
      - loose single units and for-rent-by-owner rows pass through untouched
      - county featured_unit rows are dropped when the same unit was found in
        its building (matched on the unit key in the URL fragment)

    fetch_soup(url) -> BeautifulSoup. Taking it as an argument keeps this
    testable without API calls.
    """
    building_urls = list(
        dict.fromkeys(
            r["listing_url"]
            for r in county_rows
            if r["record_type"] == "property"
            and r["listing_url"]
            and "#" not in r["listing_url"]
            and "sq ft"
            not in (
                r.get("bed_bath_sqft") or ""
            )  # loose units mis-tagged "property" (pre-fix CSVs)
        )
    )
    if max_buildings is not None:
        building_urls = building_urls[:max_buildings]
    print(f"  -> stage two: {len(building_urls)} building page(s) to fetch")

    accessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    units_by_building: dict[str, list[dict]] = {}
    failed, unparsed = [], []

    for i, b_url in enumerate(building_urls, 1):
        if i > 1:
            time.sleep(delay)
        try:
            soup = fetch_soup(b_url)
        except Exception as e:
            print(f"  -> [{i}/{len(building_urls)}] FAILED {b_url}: {e}")
            failed.append(b_url)
            continue
        units = parse_property_page(soup, b_url, accessed_at)
        if units is None:
            print(f"  -> [{i}/{len(building_urls)}] no pricing grid on {b_url}")
            unparsed.append(b_url)
            continue
        units_by_building[b_url] = units
        print(f"  -> [{i}/{len(building_urls)}] {len(units)} units  {b_url}")

    found_keys = {u["unit_id"] for units in units_by_building.values() for u in units}

    out = []
    emitted = set()
    for row in county_rows:
        if row["record_type"] == "property" and row["listing_url"] in units_by_building:
            if row["listing_url"] not in emitted:  # insert the unit rows once, in place
                out.extend(units_by_building[row["listing_url"]])
                emitted.add(row["listing_url"])
            continue  # drop this band row
        if (
            row["record_type"] == "featured_unit"
            and _unit_key_from_url(row["listing_url"]) in found_keys
        ):
            continue
        out.append(row)

    print(
        f"  -> stage two done: {len(units_by_building)} buildings expanded, "
        f"{len(failed)} failed, {len(unparsed)} without a pricing grid"
    )
    if failed or unparsed:
        print("     buildings left as band rows:", failed + unparsed)
    return out


def scrape_buildings(
    county_rows: list[dict],
    api_key: str,
    max_buildings: int | None = None,
    delay: float = 1.0,
    render_js: bool = True,
    retries: int = 3,
) -> list[dict]:
    """Scrapfly wrapper around enrich_with_building_units, with retries."""
    scrapfly = ScrapflyClient(key=api_key)

    def fetch_soup(target_url: str):
        last = None
        for attempt in range(retries):
            try:
                result = scrapfly.scrape(
                    ScrapeConfig(
                        url=target_url,
                        asp=True,
                        render_js=render_js,
                        country="us",
                    )
                )
                return BeautifulSoup(result.content, "lxml")
            except Exception as e:
                last = e
                time.sleep(5 * (attempt + 1))
        raise last

    return enrich_with_building_units(
        county_rows, fetch_soup, max_buildings=max_buildings, delay=delay
    )


# ------------------ Transform -------------------


# Replacement for export_to_csv: uses the fixed FIELDNAMES so rows from
# different parsers (building / address / county) can share one CSV.
def export_to_csv(listings: list[dict], filepath: str) -> None:
    if not listings:
        print("No listings to export.")
        return

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=FIELDNAMES, restval="", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(listings)

    print(f"Wrote {len(listings)} listing(s) to {filepath}")


# transform
# make this into a txt file temporarily
# with open('island-colony.txt', 'w') as f:
#     f.write(str(result.content))
