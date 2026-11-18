from scrape import export_to_csv, build_apartments_url, scrape_listings, build_county_url
from dotenv import load_dotenv
import os
import pandas as pd

loaded = load_dotenv(verbose=True)
print(f"Loaded: {loaded}")


api_key = os.getenv("API_TEST")  # swap with API_KEY when ready


# i need to read in the master list of units
main_address = pd.read_excel(
    "data/manual-tmk-data.ods", engine="odf", sheet_name="README"
)

list_addresses = main_address["SITE ADDRESS"]

# Optional: if your ods sheet also has a "CONDOMINIUM PROJECT NAME" column
# (matching master_tmk_address.csv), we use it to flag cases where
# apartments.com's address-based routing resolved to a building profile
# page whose name doesn't look like a match -- a sign the address matched
# the wrong community, or that the URL slug needs a manual fix.
has_project_names = "CONDOMINIUM PROJECT NAME" in main_address.columns
project_names = main_address["CONDOMINIUM PROJECT NAME"] if has_project_names else None

listings = []
mismatches = []

for i, address in enumerate(list_addresses):
    print(f"Reading available rentals for: {address}")
    url = build_apartments_url(address, "Honolulu", "HI")

    try:
        new_listing = scrape_listings(url, api_key)
        listings.extend(new_listing)
        print(f"Finished reading, number of rentals: {len(listings)}")

        if (
            has_project_names
            and new_listing
            and new_listing[0]["page_type"] == "building_profile"
        ):
            expected_name = str(project_names.iloc[i]).strip().lower()
            resolved_name = new_listing[0]["building_name"].strip().lower()
            # loose containment check -- names rarely match character-for-character
            if (
                expected_name
                and expected_name not in resolved_name
                and resolved_name not in expected_name
            ):
                print(
                    f"  !! name mismatch: expected '{expected_name}', got '{resolved_name}' -- verify this one manually"
                )
                mismatches.append((address, expected_name, resolved_name))

    except:
        print(f"Could not retrieve results for: {address}, continuing.")
        continue

export_to_csv(listings, "data/condo_listings.csv")

if mismatches:
    print(f"\n{len(mismatches)} building name mismatch(es) to review:")
    for address, expected_name, resolved_name in mismatches:
        print(f"  {address}: expected '{expected_name}', resolved to '{resolved_name}'")

# url = build_apartments_url("445 Seaside Ave", "Honolulu", "HI")
# url = build_county_url("Honolulu", "HI")
# listings = scrape_listings(url, api_key)
# export_to_csv(listings, "data/all_listings.csv")
