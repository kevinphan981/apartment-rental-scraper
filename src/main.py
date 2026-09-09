from scrape import export_to_csv, build_apartments_url, scrape_listings
from dotenv import load_dotenv
import os
import pandas as pd
loaded = load_dotenv(verbose = True)
print(f"Loaded: {loaded}")


api_key = os.getenv("API_TEST") # swap with API_KEY when ready


# i need to read in the master list of units
main_address = pd.read_excel('data/manual-tmk-data.ods', engine='odf', sheet_name ='README')

list_addresses = main_address["SITE ADDRESS"]
listings = []

for address in list_addresses:
    print(f"Reading available rentals for: {address}")
    url = build_apartments_url(address, "Honolulu", "HI")

    try:
        new_listing = scrape_listings(url, api_key)
        listings.extend(new_listing)
        print(f"Finished reading, number of rentals: {len(listings)}")
    except: 
        print(f"Could not retrieve results for: {address}, continuing.")
        continue

export_to_csv(listings, "data/condo_listings.csv")

# url = build_apartments_url("445 Seaside Ave", "Honolulu", "HI")
# listings = scrape_listings(url, api_key)
# export_to_csv(listings, "data/condo_listings.csv")