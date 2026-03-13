"""
Download and chunk MTG Comprehensive Rules for RAG pipeline.

Downloads the rules text from Wizards of the Coast, parses it into
section-aware chunks suitable for embedding and retrieval.

Usage:
    python3 rules_fetcher.py
    python3 rules_fetcher.py --url https://media.wizards.com/...
"""

import argparse
import json
import os
import re
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
RULES_FILE = os.path.join(DATA_DIR, "mtg_rules.txt")
RULINGS_FILE = os.path.join(DATA_DIR, "scryfall_rulings.json")
CHUNKS_FILE = os.path.join(DATA_DIR, "rules_chunks.json")

# WotC comprehensive rules URL (updated periodically)
DEFAULT_RULES_URL = "https://media.wizards.com/2026/downloads/MagicCompRules%2020260227.txt"

# Scryfall bulk data API for rulings
SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data"


def download_rules(url: str, output_path: str):
    """Download comprehensive rules text file."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    headers = {"User-Agent": "MTGSynergyGraph/1.0"}
    req = urllib.request.Request(url, headers=headers)

    print(f"Downloading rules from {url}...")
    with urllib.request.urlopen(req) as resp:
        content = resp.read()

    with open(output_path, "wb") as f:
        f.write(content)

    size_kb = len(content) / 1024
    print(f"Saved to {output_path} ({size_kb:.0f} KB)")
    return content.decode("utf-8", errors="replace")


def parse_rules(text: str) -> list[dict]:
    """Parse comprehensive rules into section-aware chunks.

    Strategy:
    - Split by 3-digit section headers (100, 101, ..., 702, etc.)
    - Group sub-rules under their parent section
    - Split large sections (e.g., 702 keyword abilities) into sub-chunks
    - Target ~500-800 tokens per chunk
    """
    lines = text.split("\n")
    chunks = []

    # Find where the actual rules content starts (after the TOC, where
    # "1. Game Concepts" appears a SECOND time, followed by actual rule text)
    rules_start = 0
    seen_game_concepts = False
    for i, line in enumerate(lines):
        if re.match(r"^1\. Game Concepts\s*$", line.strip()):
            if seen_game_concepts:
                # Second occurrence — this is the real content
                rules_start = i
                break
            seen_game_concepts = True

    # Find where glossary starts (end of rules proper)
    # Look for "Glossary" appearing after the rules content
    rules_end = len(lines)
    for i in range(rules_start + 1, len(lines)):
        if lines[i].strip() == "Glossary":
            rules_end = i
            break

    # Parse into sections by 3-digit rule numbers
    # The file has blank lines between rules, so we need to handle that
    current_section = None
    current_title = ""
    current_lines = []
    sections = []

    for line in lines[rules_start:rules_end]:
        stripped = line.strip()

        # Skip blank lines but don't end sections
        if not stripped:
            continue

        # Skip chapter headers like "1. Game Concepts", "2. Parts of a Card"
        if re.match(r"^\d+\.\s+[A-Z]", stripped) and not re.match(r"^\d{3}\.", stripped):
            continue

        # Match section header: "100. General" or "702. Keyword Abilities"
        section_match = re.match(r"^(\d{3})\.\s+(.+)$", stripped)
        if section_match:
            # Save previous section
            if current_section and current_lines:
                sections.append({
                    "section": current_section,
                    "title": current_title,
                    "lines": current_lines,
                })
            current_section = section_match.group(1)
            current_title = section_match.group(2)
            current_lines = [stripped]
            continue

        # Match sub-rule: "100.1." or "702.15a" or continuation text
        if re.match(r"^\d{3}\.\d+", stripped):
            current_lines.append(stripped)
        elif current_lines:
            # Continuation of previous rule (wrapped text)
            current_lines[-1] += " " + stripped

    # Save last section
    if current_section and current_lines:
        sections.append({
            "section": current_section,
            "title": current_title,
            "lines": current_lines,
        })

    # Convert sections to chunks, splitting large ones
    MAX_CHARS = 2000  # ~500 tokens

    for section in sections:
        text_block = "\n".join(section["lines"])

        if len(text_block) <= MAX_CHARS:
            # Small section — one chunk
            chunks.append({
                "id": len(chunks),
                "section": section["section"],
                "title": section["title"],
                "text": text_block,
            })
        else:
            # Large section — split by sub-rules
            sub_chunks = _split_large_section(section, MAX_CHARS)
            for sc in sub_chunks:
                sc["id"] = len(chunks)
                chunks.append(sc)

    return chunks


def _split_large_section(section: dict, max_chars: int) -> list[dict]:
    """Split a large section into sub-chunks by sub-rule boundaries."""
    header = f"{section['section']}. {section['title']}"
    chunks = []
    current_lines = [header]
    current_sub = ""
    current_len = len(header)

    for line in section["lines"][1:]:  # skip header (already added)
        # Detect sub-rule start
        sub_match = re.match(r"^(\d{3}\.\d+)", line)

        if sub_match and current_len + len(line) > max_chars and len(current_lines) > 1:
            # Save current chunk
            chunks.append({
                "section": f"{section['section']}.{current_sub}" if current_sub else section["section"],
                "title": section["title"],
                "text": "\n".join(current_lines),
            })
            current_lines = [header]
            current_len = len(header)

        if sub_match:
            current_sub = sub_match.group(1).split(".")[-1] if "." in sub_match.group(1) else ""

        current_lines.append(line)
        current_len += len(line) + 1

    # Save remaining
    if len(current_lines) > 1:
        chunks.append({
            "section": f"{section['section']}.{current_sub}" if current_sub else section["section"],
            "title": section["title"],
            "text": "\n".join(current_lines),
        })

    return chunks


def parse_glossary(text: str) -> list[dict]:
    """Parse the glossary section into chunks (one per term).

    Glossary format: terms and definitions separated by blank lines.
    A new term starts after a blank line, and the first line is the term name.
    """
    lines = text.split("\n")
    chunks = []

    # Find the LAST "Glossary" heading (after the rules content, not the TOC)
    glossary_start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "Glossary":
            glossary_start = i + 1
            break

    if not glossary_start:
        return []

    # Find end of glossary (Credits section)
    glossary_end = len(lines)
    for i in range(glossary_start, len(lines)):
        if lines[i].strip() == "Credits":
            glossary_end = i
            break

    # Group glossary entries — blank lines separate entries
    current_lines = []

    for line in lines[glossary_start:glossary_end]:
        stripped = line.strip()
        if not stripped:
            if current_lines:
                term = current_lines[0]
                chunks.append({
                    "id": len(chunks),
                    "section": "glossary",
                    "title": term,
                    "text": "\n".join(current_lines),
                })
                current_lines = []
            continue
        current_lines.append(stripped)

    if current_lines:
        chunks.append({
            "id": len(chunks),
            "section": "glossary",
            "title": current_lines[0],
            "text": "\n".join(current_lines),
        })

    return chunks


def download_scryfall_rulings(output_path: str):
    """Download Scryfall rulings bulk data via the bulk-data API."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    headers = {"User-Agent": "MTGSynergyGraph/1.0", "Accept": "application/json"}

    # Get bulk data catalog to find rulings download URL
    print("Fetching Scryfall bulk-data catalog...")
    req = urllib.request.Request(SCRYFALL_BULK_URL, headers=headers)
    with urllib.request.urlopen(req) as resp:
        catalog = json.loads(resp.read())

    rulings_entry = None
    for entry in catalog["data"]:
        if entry["type"] == "rulings":
            rulings_entry = entry
            break

    if not rulings_entry:
        raise RuntimeError("Could not find 'rulings' in Scryfall bulk-data catalog")

    download_uri = rulings_entry["download_uri"]
    print(f"Downloading rulings from {download_uri}...")

    req = urllib.request.Request(download_uri, headers=headers)
    with urllib.request.urlopen(req) as resp:
        with open(output_path, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"Saved to {output_path} ({size_mb:.1f} MB)")


def parse_scryfall_rulings(rulings_path: str, candidates_path: str = None) -> list[dict]:
    """Parse Scryfall rulings into chunks grouped by oracle_id.

    If candidates_path is provided, only include rulings for candidate cards.
    Otherwise includes all rulings.

    Each chunk = all rulings for one card, formatted as:
      "Rulings for <card_name>:
       - <ruling 1>
       - <ruling 2>"
    """
    with open(rulings_path) as f:
        rulings = json.load(f)

    # Optionally filter to candidate cards only
    candidate_ids = None
    if candidates_path and os.path.exists(candidates_path):
        with open(candidates_path) as f:
            candidates = json.load(f)
        candidate_ids = {c["oracle_id"] for c in candidates}

    # Group rulings by oracle_id
    by_oracle = {}
    for r in rulings:
        oid = r.get("oracle_id", "")
        if not oid:
            continue
        if candidate_ids and oid not in candidate_ids:
            continue
        if oid not in by_oracle:
            by_oracle[oid] = []
        by_oracle[oid].append(r.get("comment", ""))

    # Build name index from card_db if available
    name_index = {}
    try:
        from card_db import CARD_DB
        for oid, card in CARD_DB.items():
            name_index[oid] = card["name"]
    except Exception:
        pass

    chunks = []
    for oid, comments in by_oracle.items():
        card_name = name_index.get(oid, oid)
        text_lines = [f"Rulings for {card_name}:"]
        for comment in comments:
            text_lines.append(f"- {comment}")

        chunks.append({
            "id": len(chunks),
            "section": f"ruling:{oid}",
            "title": f"Rulings: {card_name}",
            "text": "\n".join(text_lines),
            "oracle_id": oid,
        })

    return chunks


def run():
    parser = argparse.ArgumentParser(description="Download and chunk MTG rules")
    parser.add_argument("--url", default=DEFAULT_RULES_URL, help="Rules text URL")
    parser.add_argument("--skip-download", action="store_true", help="Use existing rules file")
    parser.add_argument("--skip-rulings", action="store_true", help="Skip Scryfall rulings download")
    args = parser.parse_args()

    # Download comprehensive rules
    if args.skip_download and os.path.exists(RULES_FILE):
        print(f"Using existing {RULES_FILE}")
        with open(RULES_FILE, "r", errors="replace") as f:
            text = f.read()
    else:
        text = download_rules(args.url, RULES_FILE)

    # Parse rules
    print("Parsing comprehensive rules into chunks...")
    rules_chunks = parse_rules(text)
    print(f"  Rules chunks: {len(rules_chunks)}")

    # Parse glossary
    glossary_chunks = parse_glossary(text)
    # Re-number glossary chunk IDs
    for gc in glossary_chunks:
        gc["id"] = len(rules_chunks) + glossary_chunks.index(gc)
    print(f"  Glossary chunks: {len(glossary_chunks)}")

    # Download and parse Scryfall rulings
    rulings_chunks = []
    if not args.skip_rulings:
        if not os.path.exists(RULINGS_FILE) or not args.skip_download:
            download_scryfall_rulings(RULINGS_FILE)

        candidates_path = os.path.join(DATA_DIR, "kyler_candidates.json")
        print("Parsing Scryfall rulings...")
        rulings_chunks = parse_scryfall_rulings(RULINGS_FILE, candidates_path)
        # Re-number IDs
        base_id = len(rules_chunks) + len(glossary_chunks)
        for rc in rulings_chunks:
            rc["id"] = base_id + rulings_chunks.index(rc)
        print(f"  Rulings chunks: {len(rulings_chunks)} (card-specific)")
    else:
        print("Skipping Scryfall rulings (--skip-rulings)")

    all_chunks = rules_chunks + glossary_chunks + rulings_chunks
    print(f"  Total chunks: {len(all_chunks)}")

    # Stats
    total_chars = sum(len(c["text"]) for c in all_chunks)
    avg_chars = total_chars / len(all_chunks) if all_chunks else 0
    print(f"  Total text: {total_chars:,} chars ({total_chars // 4:,} est. tokens)")
    print(f"  Avg chunk: {avg_chars:.0f} chars ({avg_chars / 4:.0f} est. tokens)")

    # Save
    with open(CHUNKS_FILE, "w") as f:
        json.dump(all_chunks, f, indent=2)
    print(f"\nSaved chunks to {CHUNKS_FILE}")


if __name__ == "__main__":
    run()
