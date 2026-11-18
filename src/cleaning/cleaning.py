import re
import pandas as pd
import numpy as np
import os
from pathlib import Path

# If your script is in src/utils/, this goes up two levels to the project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

listings_path = PROJECT_ROOT / "data" / "all_listings.csv"
address_path = PROJECT_ROOT / "data" / "dpp-addresses-tmks.csv"
# the intention here is to merge the all-listings.csv with dpp-addresses-tmks.csv
# print(os.getcwd()) # returns this current directory thank god
listings = pd.read_csv(listings_path)
address = pd.read_csv(address_path)

# we ideally merge address > listings datasets here for efficiency
# merge by... address, which needs to be cleaned
address = address[["TMK", "TAXPIN", "GEOCODEADD", "CITY", "ZIP", "x", "y"]]
address = address.dropna(subset=["GEOCODEADD"])

# remove the city and zip from listings
print(listings.dtypes)
listings = listings.dropna(subset=["address"])
print(listings["address"].isna().sum())
listings["address_clean"] = listings["address"].str.split(",").str[0]


def slugify(text: str) -> str:
    text: str = text.strip().lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)  # drop punctuation
    text = re.sub(r"\s+", "-", text)  # spaces to hyphens
    text = re.sub(r"-", "", text)  # hyphens to nothing
    return text


def slugify_column(column: "pd.Series[str]") -> "pd.Series[str]":
    return pd.Series(
        [slugify(value) for value in column],
        index=column.index,
        name=column.name,
    )


listings["address_clean"] = slugify_column(listings["address_clean"])
address["GEOCODEADD"] = slugify_column(address["GEOCODEADD"])


print(listings["address_clean"].head(n=5))
print(address["GEOCODEADD"].head(n=5))
df = pd.merge(listings, address, how="left", left_on="address_clean", right_on="GEOCODEADD")
df = df.dropna(subset=["address_clean", "GEOCODEADD"])
print(df.shape)

output_path = PROJECT_ROOT / "data" / "apt.com-address-tmk.csv"
df.to_csv(output_path)
