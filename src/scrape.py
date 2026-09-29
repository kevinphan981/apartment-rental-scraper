# WARNING: PLEASE USE TEST-API before production usage or anything else. 
from scrapfly import ScrapflyClient, ScrapeConfig
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import os
import re
import csv
loaded = load_dotenv(verbose = True)
print(f"Loaded: {loaded}")
api_key = os.getenv("API_TEST") # swap with API_KEY when ready

# ----------------- URL PREP ----------------------

#TODO: make a method that changes the URL based on my master CSV

def build_apartments_url(street_address: str, city: str, state: str) -> str:
    """
    Query using the apartment (general or specific) with the city and state
    this is how the url is built:
    """

    def slugify(text: str) -> str:
        text = text.strip().lower()
        text = re.sub(r"[^a-z0-9\s-]", "", text) #drop punctuation
        text = re.sub(r"\s+", "-", text) # spaces to hyphens
        return text

    street_slug = slugify(street_address)
    city_state_slug = f"{slugify(city)}-{slugify(state)}"

    return f"https://www.apartments.com/{street_slug}_{city_state_slug}/"

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

    if re.search(rf"\bunit\s*#?\s*{re.escape(unit_number)}\b", street_address, re.IGNORECASE):
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
        available_date = available_date_el.get_text(strip=True) if available_date_el else ""

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

        listings.append({
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
        })

    return listings


def parse_search_page(soup, url: str, accessed_at: str) -> list[dict]:
    """Parse the generic 'near address' search-results template (article.placard)."""
    listings = []

    for article in soup.select("article.placard"):
        listing_id = article.get("data-listingid", "")
        street_address = article.get("data-streetaddress", "")
        unit_number = article.get("data-unitnumber", "")
        listing_url = article.get("data-url", "")

        title_el = article.select_one(".property-title, .js-placardTitle")
        full_title = title_el.get_text(strip = True) if title_el else ""


        # we have two different types of placard layouts for single unit + multi unit (the default building view)
        bed_rent_boxes = article.select(".bedRentBox")

        if bed_rent_boxes:
            for box in bed_rent_boxes:
                bed_text_el = box.select_one(".bedTextBox")
                price_el = box.select_one(".priceTextBox")
                listings.append({
                    "listing_id" : listing_id,
                    "page_type": "search_results",
                    "building_name": full_title,
                    "address" : build_full_address(street_address, unit_number),
                    "unit_number": unit_number,
                    "is_placeholder": is_placeholder_unit(unit_number),
                    "rent" : price_el.get_text(strip = True) if price_el else "",
                    "bed_bath_sqft" : bed_text_el.get_text(strip = True) if bed_text_el else "",
                    "lease_term": "",
                    "available_date": "",
                    "listing_url": listing_url,
                    "source_page_url": url,
                    "canonical_url": "",
                    "building_available_units_label": "",
                    "date_accessed": accessed_at,
                })
        else:
            price_el = article.select_one(".property_pricing")
            bed_range_el = article.select_one(".bed-range")
            bed_bath = _clean_bed_bath_sqft(bed_range_el)

            listings.append({
                "listing_id" : listing_id,
                "page_type": "search_results",
                "building_name": full_title,
                "address" : build_full_address(street_address, unit_number),
                "unit_number": unit_number,
                "is_placeholder": is_placeholder_unit(unit_number),
                "rent" : price_el.get_text(strip = True) if price_el else "",
                "bed_bath_sqft" : bed_bath,
                "lease_term": "",
                "available_date": "",
                "listing_url": listing_url,
                "source_page_url": url,
                "canonical_url": "",
                "building_available_units_label": "",
                "date_accessed": accessed_at,
            })
    return listings


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
        result = scrapfly.scrape(ScrapeConfig(
            url=target_url,
            asp=True,
            render_js=True,
            country="us",
        ))
        return BeautifulSoup(result.content, "lxml")

    accessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    soup = fetch_soup(url)

    building_listings = parse_building_page(soup, url, accessed_at)
    if building_listings is not None:
        print(f"  -> resolved directly to a building profile page ({len(building_listings)} units)")
        return building_listings

    search_listings = parse_search_page(soup, url, accessed_at)
    print(f"  -> resolved to a search-results page ({len(search_listings)} listings)")

    building_url = find_building_profile_url(soup)

    if building_url is None:
        candidate_urls = list(dict.fromkeys(
            row["listing_url"] for row in search_listings if row["listing_url"]
        ))[:max_probe_pages]

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
    fuller_listings = parse_building_page(building_soup, building_url, accessed_at) or []
    print(f"  -> building profile at {building_url} added {len(fuller_listings)} units")

    # merge, de-duped by unit number; the building-profile copy wins when a
    # unit shows up in both (it carries lease_term/available_date the
    # search-results template doesn't)
    merged = {row["unit_number"]: row for row in search_listings if row["unit_number"]}
    merged.update({row["unit_number"]: row for row in fuller_listings if row["unit_number"]})
    return list(merged.values())


# ------------------ Transform -------------------

def export_to_csv(listings: list[dict], filepath: str) -> None:
    if not listings:
        print("No listings to export.")
        return

    fieldnames = list(listings[0].keys())
    with open(filepath, "w", newline = "", encoding = "utf-8") as f:
        writer = csv.DictWriter(f, fieldnames = fieldnames)
        writer.writeheader()
        writer.writerows(listings)

    print(f"Wrote {len(listings)} listing(s) to {filepath}")


    
# transform
# make this into a txt file temporarily
# with open('island-colony.txt', 'w') as f:
#     f.write(str(result.content))