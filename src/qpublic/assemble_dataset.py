
import argparse
import json
import os
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
import uuid 

from qpublic_master import get_master_tmks_from_csv, fetch_many_masters
from qpublic_unit import fetch_and_parse_parcel

'''
    The script does the following: 
    1. Takes the master list of TMKs from the ODS (LibreOffice, Excel-compatible) file that I have
    2. Cross-references the list with the qpublic-master.py file in order to grab each building's list of TMKs
    3. Builds a larger dataframe of the thing
    4. Checkpoints to a parquet file because it is quite long

'''

os.chdir('')
CHECKPOINT_DIR = "/home/kevinphan981/Programming/apartment-rental-scraper/data"



def main():
    # Full batch run over every master TMK already in the CSV -- returns a DataFrame
    # directly (master_tmk, parcel_number, unit_number, modified_address), while
    # checkpointing progress to master_units.jsonl as it goes:
    masters = get_master_tmks_from_csv()
    df = fetch_many_masters(masters, out_path="master_units.jsonl")
    df.to_parquet("data/master_units.parquet", index=False)  # optional, if you still want a file


    # we then fetch everything and then convert it into a dataframe (a new one)

    for parcel in df['parcel_number']:
        fetch_and_parse_parcel(parcel)


    # we export to hopefully merge it with the apartments.com data later on.



if __name__ == "__main__":
    main()