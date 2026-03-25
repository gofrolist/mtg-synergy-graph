#!/usr/bin/env python3
"""CLI wrapper for Forge DSL import.

Usage:
    python3 import_forge.py --download    # Clone Forge repo (sparse)
    python3 import_forge.py --import      # Full import with SVar resolution
    python3 import_forge.py --stats       # Show import stats
    python3 import_forge.py --map         # Build name mapping to Scryfall
"""
import argparse
import subprocess
import os

FORGE_REPO = "https://github.com/Card-Forge/forge.git"
FORGE_DIR = "data/forge"


def download_forge():
    if os.path.exists(FORGE_DIR):
        print(f"Forge repo already exists at {FORGE_DIR}")
        return
    print("Cloning Forge repo (sparse, cardsfolder only)...")
    subprocess.run([
        "git", "clone", "--depth", "1", "--filter=blob:none",
        "--sparse", FORGE_REPO, FORGE_DIR
    ], check=True)
    subprocess.run([
        "git", "-C", FORGE_DIR, "sparse-checkout", "set",
        "forge-gui/res/cardsfolder"
    ], check=True)
    print("Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--import", dest="do_import", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--map", action="store_true", help="Build name mapping")
    args = parser.parse_args()

    if args.download:
        download_forge()
        return

    from mtg_synergy.db import get_connection
    from mtg_synergy.parse.forge_import import import_all, show_stats, build_name_mapping

    conn = get_connection()
    if args.do_import:
        import_all(conn)
    elif args.stats:
        show_stats(conn)
    elif args.map:
        build_name_mapping(conn)
    else:
        parser.print_help()
    conn.close()


if __name__ == "__main__":
    main()
