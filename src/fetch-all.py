from scrape import export_to_csv, scrape_county, scrape_buildings
from dotenv import load_dotenv
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = PROJECT_ROOT / "data" / "all_listings.csv"

loaded = load_dotenv(verbose=True)
print(f"Loaded: {loaded}")

api_key = os.getenv("API_TEST")  # swap with API_KEY when ready

# for some reason we're only getting half of what we want
listings = scrape_county("Honolulu", "HI", api_key, max_pages=None)
listings = scrape_buildings(listings, api_key, max_buildings=None)
export_to_csv(listings, filepath=str(DATA_PATH))
