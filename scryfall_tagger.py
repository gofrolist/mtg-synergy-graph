"""
Fetch function tags from Scryfall Tagger via GraphQL API.

Downloads community-curated oracle/function tags for candidate cards.
These tags replace LLM-generated synergy_tags as the canonical tag source.

Usage:
    python3 scryfall_tagger.py
    python3 scryfall_tagger.py --input data/kyler_candidates.json
    python3 scryfall_tagger.py --resume        # continue interrupted run
    python3 scryfall_tagger.py --dry-run       # show what would be fetched
"""

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "scryfall_function_tags.json")

TAGGER_URL = "https://tagger.scryfall.com"
GRAPHQL_URL = f"{TAGGER_URL}/graphql"
SCRYFALL_API = "https://api.scryfall.com"

# Tags to exclude — these are meta/trivia, not functional
EXCLUDE_TAG_PREFIXES = ["cycle-", "wild pair"]
EXCLUDE_TAGS = {
    "7ph", "3points", "oversized", "meme", "commonly nicknamed",
    "single english word name", "bible reference", "virtual french vanilla",
    "interrupt",
}


def get_csrf_and_cookies() -> tuple[str, str]:
    """Fetch CSRF token and session cookie from tagger homepage."""
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    req = urllib.request.Request(TAGGER_URL, headers={"User-Agent": "MTGSynergyGraph/1.0"})
    resp = opener.open(req)
    html = resp.read().decode("utf-8", errors="replace")

    match = re.search(r'csrf-token.*?content="([^"]*)"', html)
    if not match:
        raise RuntimeError("Could not find CSRF token in tagger page")

    csrf = match.group(1)

    # Extract cookie header
    cookie_header = "; ".join(f"{c.name}={c.value}" for c in cookie_jar)
    return csrf, cookie_header


def graphql_query(query: str, csrf: str, cookies: str) -> dict:
    """Execute a GraphQL query against the tagger API."""
    payload = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Cookie": cookies,
            "User-Agent": "MTGSynergyGraph/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def resolve_printings(candidates: list[dict]) -> dict[str, dict]:
    """Resolve oracle_id -> set/number via Scryfall search API.

    Uses curl subprocess to avoid urllib SSL issues on some systems.
    """
    printings = {}
    total = len(candidates)

    for i, card in enumerate(candidates):
        name = card["name"]
        oracle_id = card["oracle_id"]

        # Strip "// duplicate_name" suffix from double-faced cards
        lookup_name = name.split(" // ")[0].strip()

        # Use Scryfall fuzzy lookup for robustness
        encoded_name = urllib.parse.quote(lookup_name)
        result = subprocess.run(
            ["curl", "-s",
             f"{SCRYFALL_API}/cards/named?fuzzy={encoded_name}",
             "-H", "User-Agent: MTGSynergyGraph/1.0"],
            capture_output=True, text=True,
        )

        try:
            data = json.loads(result.stdout)
            if data.get("object") == "error":
                print(f"  [{i+1}/{total}] {name}: not found on Scryfall", file=sys.stderr)
                continue
            printings[oracle_id] = {
                "name": name,
                "set": data["set"],
                "number": data["collector_number"],
            }
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  [{i+1}/{total}] {name}: resolve error: {e}", file=sys.stderr)
            continue

        if (i + 1) % 50 == 0:
            print(f"  Resolved {i+1}/{total} printings...")

        time.sleep(0.1)  # Scryfall rate limit: 10 req/s

    return printings


def filter_tags(taggings: list[dict]) -> list[dict]:
    """Filter taggings to only useful function tags."""
    result = []
    for t in taggings:
        if t["classifier"] != "ORACLE_CARD_TAG":
            continue
        if t["status"] != "GOOD_STANDING":
            continue
        name = t["tag"]["name"]
        if name in EXCLUDE_TAGS:
            continue
        if any(name.startswith(p) for p in EXCLUDE_TAG_PREFIXES):
            continue
        result.append({
            "name": name,
            "weight": t["weight"],
        })
    return result


def fetch_card_tags(set_code: str, number: str, csrf: str, cookies: str) -> list[dict] | None:
    """Fetch function tags for a single card via GraphQL."""
    query = (
        f'{{ cardBySet(set: "{set_code}", number: "{number}") '
        f'{{ name oracleId taggings {{ tag {{ name type }} classifier weight status }} }} }}'
    )

    try:
        data = graphql_query(query, csrf, cookies)
    except Exception as e:
        return None

    card = data.get("data", {}).get("cardBySet")
    if not card:
        return None

    return filter_tags(card.get("taggings", []))


def run():
    parser = argparse.ArgumentParser(description="Fetch Scryfall tagger function tags")
    parser.add_argument("--input", default=os.path.join(DATA_DIR, "kyler_candidates.json"),
                        help="Candidates JSON file")
    parser.add_argument("--output", default=OUTPUT_FILE, help="Output file path")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without fetching")
    parser.add_argument("--refresh-csrf", type=int, default=50,
                        help="Refresh CSRF token every N requests")
    args = parser.parse_args()

    # Load candidates
    with open(args.input) as f:
        candidates = json.load(f)
    print(f"Loaded {len(candidates)} candidates from {args.input}")

    # Load existing results for resume
    existing = {}
    if os.path.exists(args.output):
        with open(args.output) as f:
            existing_list = json.load(f)
        existing = {item["oracle_id"]: item for item in existing_list}
        print(f"Found {len(existing)} already-fetched cards — will skip them")

    remaining = [c for c in candidates if c["oracle_id"] not in existing]
    print(f"Cards to fetch: {len(remaining)}")

    if args.dry_run:
        print(f"\n--- DRY RUN ---")
        print(f"Would fetch tags for {len(remaining)} cards")
        print(f"Output: {args.output}")
        print(f"Estimated time: ~{len(remaining) * 0.25:.0f}s ({len(remaining) * 0.25 / 60:.1f}min)")
        return

    if not remaining:
        print("Nothing to fetch!")
        return

    # Resolve set/number for remaining cards
    print("\nResolving card printings via Scryfall API...")
    printings = resolve_printings(remaining)
    print(f"Resolved {len(printings)}/{len(remaining)} printings")

    # Get CSRF token
    print("\nFetching CSRF token from tagger...")
    csrf, cookies = get_csrf_and_cookies()
    print("Got CSRF token")

    # Fetch tags
    all_results = list(existing.values())
    fetched = 0
    errors = 0

    for i, card in enumerate(remaining):
        oracle_id = card["oracle_id"]
        name = card["name"]

        printing = printings.get(oracle_id)
        if not printing:
            errors += 1
            continue

        # Refresh CSRF periodically
        if fetched > 0 and fetched % args.refresh_csrf == 0:
            try:
                csrf, cookies = get_csrf_and_cookies()
                print(f"  (refreshed CSRF token)")
            except Exception:
                pass  # keep using old token

        tags = fetch_card_tags(printing["set"], printing["number"], csrf, cookies)

        if tags is None:
            print(f"  [{i+1}/{len(remaining)}] {name}: ERROR fetching tags")
            errors += 1
            # Still record it with empty tags
            all_results.append({
                "oracle_id": oracle_id,
                "name": name,
                "scryfall_tags": [],
            })
        else:
            tag_names = [t["name"] for t in tags]
            all_results.append({
                "oracle_id": oracle_id,
                "name": name,
                "scryfall_tags": tag_names,
                "scryfall_tags_detail": tags,
            })
            fetched += 1

            if fetched <= 5 or fetched % 50 == 0:
                print(f"  [{i+1}/{len(remaining)}] {name}: {tag_names}")

        # Save periodically (crash-safe)
        if (fetched + errors) % 25 == 0:
            with open(args.output, "w") as f:
                json.dump(all_results, f, indent=2)

        time.sleep(0.15)  # rate limit

    # Final save
    with open(args.output, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n{'='*60}")
    print(f"DONE: {fetched} fetched, {errors} errors, {len(existing)} skipped (already done)")
    print(f"Total in {args.output}: {len(all_results)}")
    print(f"{'='*60}")

    # Print tag stats
    all_tags = []
    for item in all_results:
        all_tags.extend(item.get("scryfall_tags", []))
    unique_tags = set(all_tags)
    print(f"\nTag stats: {len(all_tags)} total tags, {len(unique_tags)} unique")

    cards_with_tags = sum(1 for item in all_results if item.get("scryfall_tags"))
    cards_without = sum(1 for item in all_results if not item.get("scryfall_tags"))
    print(f"Cards with tags: {cards_with_tags}, without: {cards_without}")


if __name__ == "__main__":
    run()
