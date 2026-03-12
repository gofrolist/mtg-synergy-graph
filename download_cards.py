"""
Download Scryfall oracle_cards bulk data to data/oracle_cards.json.

Usage:
    python3 download_cards.py
"""

import json
import os
import urllib.request


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "oracle_cards.json")
BULK_DATA_URL = "https://api.scryfall.com/bulk-data"


def download():
    os.makedirs(DATA_DIR, exist_ok=True)

    headers = {"User-Agent": "MTGDeckOptimizer/1.0", "Accept": "application/json"}

    print("Fetching bulk-data catalog...")
    req = urllib.request.Request(BULK_DATA_URL, headers=headers)
    with urllib.request.urlopen(req) as resp:
        catalog = json.loads(resp.read())

    oracle_entry = None
    for entry in catalog["data"]:
        if entry["type"] == "oracle_cards":
            oracle_entry = entry
            break

    if not oracle_entry:
        raise RuntimeError("Could not find 'oracle_cards' in bulk-data catalog")

    download_uri = oracle_entry["download_uri"]
    print(f"Downloading oracle_cards from {download_uri} ...")

    req = urllib.request.Request(download_uri, headers=headers)
    with urllib.request.urlopen(req) as resp:
        with open(OUTPUT_FILE, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)

    # Verify it's valid JSON and report stats
    with open(OUTPUT_FILE, "r") as f:
        cards = json.load(f)

    print(f"Downloaded {len(cards)} cards to {OUTPUT_FILE}")
    print(f"File size: {os.path.getsize(OUTPUT_FILE) / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    download()
