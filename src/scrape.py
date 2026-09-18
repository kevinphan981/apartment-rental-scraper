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
        text = re.sub(F"\s+", "-", text) # spaces to hyphens
        return text

    street_slug = slugify(street_address)
    city_state_slug = f"{slugify(city)}-{slugify(state)}"

    return f"https://www.apartments.com/{street_slug}_{city_state_slug}/"

# ------------------- Scrapify Scrape --------------
# it works, now we have to fetch the real information...
# URL = "https://www.apartments.com/445-seaside-ave_honolulu-hi/?rid=6jtjznv"

def scrape_listings(url: str, api_key: str) -> list[dict]:
    scrapfly = ScrapflyClient(key=api_key)
    result = scrapfly.scrape(ScrapeConfig(
        url= url,
        asp=True,
        render_js=True,
        country="us",
    ))
    

    # we going to use bs4 to do this part
    html = result.content 
    soup = BeautifulSoup(html, "lxml")
    listings = []
    accessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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
                    "address" : full_title or street_address,
                    "unit_number": unit_number,
                    "rent" : price_el.get_text(strip = True) if price_el else "",
                    "bed_bath_sqft" : bed_text_el.get_text(strip = True) if bed_text_el else "",
                    "listing_url": listing_url,
                    "date_accessed": accessed_at,
                })
        else:
            price_el = article.select_one(".property_pricing")
            bed_range_el = article.select_one(".bed-range")
            bed_bath = ""
            if bed_range_el:
                bed_bath = " | ".join(
                    s.strip() for s in bed_range_el.stripped_strings if s.strip() != "|"
                )

            listings.append({
                "listing_id" : listing_id,
                "address" : street_address,
                "unit_number": unit_number,
                "rent" : price_el.get_text(strip = True) if price_el else "",
                "bed_bath_sqft" : bed_bath, 
                "listing_url": listing_url,
                "date_accessed": accessed_at,
            })
    return listings


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