#!/usr/bin/env python3
"""Reclassify generic parent tags into specific sub-tags using OpenAI Batch API.

Replaces broad tags (e.g. creature-pump) with specific sub-tags (e.g. pump-lord,
pump-anthem) to improve synergy matching precision.

Usage:
    # Dry run — show counts without changing DB
    python3 reclassify_tags.py --all --dry-run

    # Reclassify one parent tag
    python3 reclassify_tags.py --parent-tag creature-pump

    # Reclassify all 6 parent tags
    python3 reclassify_tags.py --all

    # Test on first N cards (uses regular API, not batch)
    python3 reclassify_tags.py --parent-tag creature-pump --test 10

    # Custom model
    python3 reclassify_tags.py --all --model gpt-4.1-mini
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# ── Tag definitions ──────────────────────────────────────────────────────

SUBTAG_MAP = {
    "creature-pump": {
        "pump-lord": "Permanently buffs creatures of a specific type",
        "pump-anthem": "Permanently buffs all/most creatures regardless of type",
        "pump-combat": "Temporary combat buffs, voltron equipment/auras",
        "pump-self": "Card grows itself via counters or scaling",
    },
    "creature-board": {
        "board-tokens": "Payoff for having token creatures specifically",
        "board-tribal": "Payoff for having creatures of a specific type on board",
        "board-go-wide": "Payoff for having many creatures (count matters)",
        "board-generic": "Needs creatures on board but not count/type dependent",
    },
    "creature-etb": {
        "etb-value": "Triggers on any creature entering for value (draw, removal, ramp)",
        "etb-tokens": "Triggers on creature entry to make tokens or deal damage",
        "etb-tribal": "Triggers on specific creature type entering",
    },
    "combat-events": {
        "combat-attack": "Triggers on attacking or beginning of combat",
        "combat-damage": "Triggers on dealing combat damage",
        "combat-block": "Triggers on blocking or cares about being blocked",
    },
    "token-generation": {
        "tokens-creature": "Creates creature tokens",
        "tokens-artifact": "Creates treasure, clue, food, or other artifact tokens",
        "tokens-tribal": "Creates tokens of a specific creature type",
    },
    "evasion-grant": {
        "evasion-flying": "Grants or has flying",
        "evasion-unblockable": "Makes creatures unblockable",
        "evasion-menace": "Grants menace, fear, intimidate, or similar partial evasion",
    },
}

TAG_TABLE = {
    "creature-pump": "provides",
    "token-generation": "provides",
    "evasion-grant": "provides",
    "creature-board": "wants",
    "creature-etb": "wants",
    "combat-events": "wants",
}

DEFAULT_SUBTAG = {
    "creature-pump": "pump-combat",
    "creature-board": "board-generic",
    "creature-etb": "etb-value",
    "combat-events": "combat-attack",
    "token-generation": "tokens-creature",
    "evasion-grant": "evasion-flying",
}

# ── Prompts ──────────────────────────────────────────────────────────────


def build_system_prompt(parent_tag: str) -> str:
    """Build system prompt for tag reclassification."""
    sub_tags = SUBTAG_MAP[parent_tag]
    sub_list = "\n".join(f"  - {tag}: {desc}" for tag, desc in sub_tags.items())
    return (
        f"You are a Magic: The Gathering card classifier. "
        f"Your task is to reclassify cards currently tagged with '{parent_tag}' "
        f"into one of these more specific sub-categories:\n{sub_list}\n\n"
        f"For each card, choose the single most specific sub-category based on the card's "
        f"oracle text and type line. Return valid JSON."
    )


def build_reclassify_prompt(parent_tag: str, cards: list[dict]) -> str:
    """Build user prompt listing cards and sub-categories."""
    sub_tags = SUBTAG_MAP[parent_tag]
    sub_list = "\n".join(f"  - {tag}: {desc}" for tag, desc in sub_tags.items())

    lines = [
        f"Reclassify these cards from '{parent_tag}' into one of these sub-categories:",
        "",
        sub_list,
        "",
        "Choose the single most specific sub-category. If the card fits multiple, "
        "pick the narrower one. For example, tokens-tribal is more specific than "
        "tokens-creature.",
        "",
        "Cards:",
        "",
    ]

    for c in cards:
        name = c.get("name", "Unknown")
        type_line = c.get("type_line", "")
        oracle = (c.get("oracle_text") or "")[:300]
        lines.append(f"{name} | {type_line} | {oracle}")

    lines.append("")
    lines.append('Return JSON: {"cards": [{"name": "Card Name", "sub_tag": "chosen-sub-tag"}]}')

    return "\n".join(lines)


# ── Response parsing ─────────────────────────────────────────────────────


def parse_reclassify_results(raw: str, parent_tag: str, expected_count: int) -> list[dict]:
    """Parse LLM JSON response into list of {name, sub_tag} dicts.

    Validates sub_tags against SUBTAG_MAP; invalid ones get DEFAULT_SUBTAG.
    """
    if not raw:
        return []

    # Strip thinking tags and markdown fences
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    # Handle {"cards": [...]} wrapper
    if isinstance(result, dict):
        for key in ("cards", "results", "data"):
            if key in result and isinstance(result[key], list):
                result = result[key]
                break
        else:
            result = [result]

    if not isinstance(result, list):
        return []

    if len(result) != expected_count:
        print(f"  [WARN] Expected {expected_count}, got {len(result)}")

    valid_tags = set(SUBTAG_MAP[parent_tag].keys())
    fallback = DEFAULT_SUBTAG[parent_tag]

    for item in result:
        if not isinstance(item, dict):
            continue
        sub = item.get("sub_tag", "")
        if sub not in valid_tags:
            item["sub_tag"] = fallback

    return [item for item in result if isinstance(item, dict) and "name" in item]


# ── DB operations ────────────────────────────────────────────────────────


def get_cards_with_tag(conn: sqlite3.Connection, parent_tag: str) -> list[dict]:
    """Query DB for cards that have the given parent tag."""
    table = TAG_TABLE[parent_tag]
    rows = conn.execute(f"""
        SELECT t.oracle_id, c.name, c.type_line, c.oracle_text
        FROM {table} t
        JOIN cards c ON c.oracle_id = t.oracle_id
        WHERE t.tag = ?
        ORDER BY c.edhrec_rank
    """, (parent_tag,)).fetchall()

    return [{
        "oracle_id": r[0],
        "name": r[1],
        "type_line": r[2] or "",
        "oracle_text": r[3] or "",
    } for r in rows]


def apply_reclassification(
    conn: sqlite3.Connection,
    parent_tag: str,
    results: list[dict],
    oracle_id_lookup: dict[str, str],
):
    """Delete old parent tag and insert new sub-tag for each card."""
    table = TAG_TABLE[parent_tag]
    applied = 0

    for item in results:
        name = item.get("name", "")
        sub_tag = item.get("sub_tag", "")
        oracle_id = oracle_id_lookup.get(name.lower())

        if not oracle_id or not sub_tag:
            continue

        conn.execute(
            f"DELETE FROM {table} WHERE oracle_id = ? AND tag = ?",
            (oracle_id, parent_tag),
        )
        conn.execute(
            f"INSERT OR IGNORE INTO {table} (oracle_id, tag) VALUES (?, ?)",
            (oracle_id, sub_tag),
        )
        applied += 1

    return applied


# ── Batch API ────────────────────────────────────────────────────────────


def run_batch_api(
    cards: list[dict],
    parent_tag: str,
    api_key: str,
    model: str,
    batch_size: int = 50,
):
    """Submit reclassification via OpenAI Batch API and apply results."""
    conn = sqlite3.connect(DB_PATH)

    system = build_system_prompt(parent_tag)
    oracle_id_lookup = {c["name"].lower(): c["oracle_id"] for c in cards}

    # Build batches
    batches = []
    for i in range(0, len(cards), batch_size):
        batches.append(cards[i:i + batch_size])

    # Build JSONL
    jsonl_lines = []
    batch_map = {}
    token_param = "max_completion_tokens" if ("gpt-5" in model or "gpt-4.1" in model) else "max_tokens"

    for batch_idx, batch in enumerate(batches):
        custom_id = f"reclassify-{parent_tag}-{batch_idx}"
        user_prompt = build_reclassify_prompt(parent_tag, batch)
        request = {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": model,
                token_param: 4096,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
            },
        }
        jsonl_lines.append(json.dumps(request))
        batch_map[custom_id] = batch

    jsonl_path = os.path.join(DATA_DIR, f"batch_reclassify_{parent_tag}.jsonl")
    with open(jsonl_path, "w") as f:
        f.write("\n".join(jsonl_lines))
    print(f"  Created {jsonl_path} ({len(jsonl_lines)} requests, {len(cards)} cards)")

    # Upload
    print("  Uploading to OpenAI...")
    boundary = "----ReclassifyBoundary"
    with open(jsonl_path, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="purpose"\r\n\r\n'
        f"batch\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="batch.jsonl"\r\n'
        f"Content-Type: application/jsonl\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/files",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        file_obj = json.loads(resp.read())
    file_id = file_obj["id"]
    print(f"  File uploaded: {file_id}")

    # Create batch
    batch_payload = json.dumps({
        "input_file_id": file_id,
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
        "metadata": {"task": f"reclassify-{parent_tag}"},
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/batches",
        data=batch_payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        batch_obj = json.loads(resp.read())
    batch_id = batch_obj["id"]
    print(f"  Batch created: {batch_id}")

    # Poll
    print("  Waiting for completion...", end="", flush=True)
    while True:
        time.sleep(30)
        req = urllib.request.Request(
            f"https://api.openai.com/v1/batches/{batch_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            status_obj = json.loads(resp.read())

        status = status_obj["status"]
        completed = status_obj.get("request_counts", {}).get("completed", 0)
        total_reqs = status_obj.get("request_counts", {}).get("total", len(batches))
        print(f"\r  Status: {status} ({completed}/{total_reqs})", end="", flush=True)

        if status in ("completed", "failed", "expired", "cancelled"):
            print()
            break

    if status != "completed":
        print(f"  Batch {status}!")
        conn.close()
        return

    # Download results
    output_file_id = status_obj.get("output_file_id")
    if not output_file_id:
        print("  No output file!")
        conn.close()
        return

    req = urllib.request.Request(
        f"https://api.openai.com/v1/files/{output_file_id}/content",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        output_data = resp.read().decode()

    # Parse and apply
    total_applied = 0
    total_failed = 0
    for line in output_data.strip().split("\n"):
        result = json.loads(line)
        custom_id = result["custom_id"]
        batch = batch_map.get(custom_id, [])

        response = result.get("response", {})
        if response.get("status_code") != 200:
            print(f"  [BATCH] {custom_id} HTTP {response.get('status_code')}")
            total_failed += len(batch)
            continue

        choice = response["body"]["choices"][0]
        raw = choice["message"]["content"].strip()
        finish_reason = choice.get("finish_reason", "unknown")

        if not raw or finish_reason == "length":
            print(f"  [BATCH] {custom_id} truncated ({len(raw)} chars)")
            total_failed += len(batch)
            continue

        parsed = parse_reclassify_results(raw, parent_tag, len(batch))
        if not parsed:
            total_failed += len(batch)
            continue

        applied = apply_reclassification(conn, parent_tag, parsed, oracle_id_lookup)
        total_applied += applied
        total_failed += len(batch) - applied

    conn.commit()
    conn.close()

    print(f"  Done: {total_applied} cards reclassified, {total_failed} failed")

    # Cleanup
    try:
        os.remove(jsonl_path)
    except OSError:
        pass


# ── Regular API (for --test mode) ────────────────────────────────────────


def run_regular(
    cards: list[dict],
    parent_tag: str,
    api_key: str,
    model: str,
    batch_size: int = 50,
):
    """Reclassify cards using regular API (for small tests)."""
    conn = sqlite3.connect(DB_PATH)

    system = build_system_prompt(parent_tag)
    oracle_id_lookup = {c["name"].lower(): c["oracle_id"] for c in cards}
    token_param = "max_completion_tokens" if ("gpt-5" in model or "gpt-4.1" in model) else "max_tokens"
    total_applied = 0

    for i in range(0, len(cards), batch_size):
        batch = cards[i:i + batch_size]
        user = build_reclassify_prompt(parent_tag, batch)

        payload = json.dumps({
            "model": model,
            token_param: 4096,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
                raw = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"  [ERROR] Batch {i // batch_size + 1}: {e}")
            continue

        parsed = parse_reclassify_results(raw, parent_tag, len(batch))
        if not parsed:
            print(f"  [ERROR] Batch {i // batch_size + 1}: parse failed")
            continue

        applied = apply_reclassification(conn, parent_tag, parsed, oracle_id_lookup)
        total_applied += applied

        batch_num = i // batch_size + 1
        total_batches = (len(cards) + batch_size - 1) // batch_size
        print(f"  Batch {batch_num}/{total_batches}: {applied} reclassified")

    conn.commit()
    conn.close()
    print(f"\n  Done: {total_applied} cards reclassified")


# ── Main ─────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Reclassify generic parent tags into specific sub-tags via OpenAI Batch API"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--parent-tag", type=str, choices=list(SUBTAG_MAP.keys()),
                       help="Reclassify one parent tag")
    group.add_argument("--all", action="store_true",
                       help="Reclassify all 6 parent tags")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show counts without changing DB")
    parser.add_argument("--test", type=int, default=0,
                        help="Process first N cards only (uses regular API)")
    parser.add_argument("--model", default="gpt-4.1-mini",
                        help="OpenAI model (default: gpt-4.1-mini)")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Cards per API request (default: 50)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key and not args.dry_run:
        print("ERROR: Set OPENAI_API_KEY environment variable")
        sys.exit(1)

    parent_tags = list(SUBTAG_MAP.keys()) if args.all else [args.parent_tag]

    conn = sqlite3.connect(DB_PATH)

    for parent_tag in parent_tags:
        cards = get_cards_with_tag(conn, parent_tag)
        table = TAG_TABLE[parent_tag]
        sub_tags = ", ".join(SUBTAG_MAP[parent_tag].keys())

        print(f"\n{'=' * 60}")
        print(f"Parent tag: {parent_tag} ({table} table)")
        print(f"Cards: {len(cards)}")
        print(f"Sub-tags: {sub_tags}")

        if args.dry_run:
            print(f"\n[DRY RUN] First 10 cards:")
            for c in cards[:10]:
                oracle_short = (c.get("oracle_text") or "")[:80]
                print(f"  {c['name']:<40} {oracle_short}")
            if len(cards) > 10:
                print(f"  ... and {len(cards) - 10} more")
            continue

        if len(cards) == 0:
            print("  No cards to reclassify.")
            continue

        target_cards = cards[:args.test] if args.test > 0 else cards

        # Cost estimate
        batches_count = (len(target_cards) + args.batch_size - 1) // args.batch_size
        input_tokens = len(target_cards) * 80
        output_tokens = len(target_cards) * 30
        cost_regular = (input_tokens * 0.40 + output_tokens * 1.60) / 1_000_000
        cost_batch = cost_regular * 0.5

        print(f"Processing: {len(target_cards)} cards in {batches_count} batches")
        print(f"Estimated cost: ${cost_regular:.3f} (regular) / ${cost_batch:.3f} (batch)")

        if args.test > 0:
            print(f"\n  Testing with {len(target_cards)} cards (regular API)...")
            run_regular(target_cards, parent_tag, api_key, args.model, args.batch_size)
        else:
            print(f"\n  Submitting to Batch API...")
            run_batch_api(target_cards, parent_tag, api_key, args.model, args.batch_size)

    conn.close()


if __name__ == "__main__":
    main()
