#!/usr/bin/env python3
"""Combined batch extraction: tags + mechanics in one API call.

Extracts both provides/wants tags and structured game mechanics for each card
in a single LLM prompt, using OpenAI Batch API for 50% cost reduction.

Usage:
    # Mechanics only (cards that have tags but not mechanics)
    python3 batch_extract.py --mechanics-only --dry-run
    python3 batch_extract.py --mechanics-only

    # Combined retag + mechanics (all target cards)
    python3 batch_extract.py --combined --dry-run
    python3 batch_extract.py --combined

    # Custom rank cutoff
    python3 batch_extract.py --mechanics-only --max-rank 10000

    # Test on N cards first
    python3 batch_extract.py --mechanics-only --test 10
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
from pathlib import Path

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Import canonical vocabulary from extract_mechanics
from extract_mechanics import ACTIONS, EVENTS

# ── Prompt ────────────────────────────────────────────────────────────────

MECHANICS_SCHEMA = f"""Extract structured game mechanics from each card.

For each card, return a JSON object with:
{{
  "name": "Card Name",
  "mechanics": [
    {{
      "type": "triggered" | "activated" | "static" | "replacement" | "keyword",
      "trigger": {{"event": "<EVENT>", "filter": {{...}} | null}},
      "cost": "<cost string>",
      "effect": {{"action": "<ACTION>", "target": "...", "amount": N, "token": {{"subtype": "...", "power": N, "toughness": N}}}},
      "modifier": {{"modifies": "<ACTION>", "how": "double" | "prevent" | "redirect"}},
      "scope": {{"subtype": "...", "card_type": "...", "controller": "you" | "opponent", "is_another": true, "has_keyword": "..."}}
    }}
  ]
}}

ALLOWED ACTIONS: {', '.join(ACTIONS)}
ALLOWED TRIGGER EVENTS: {', '.join(EVENTS)}

Rules:
- Use ONLY the allowed actions and events
- For "whenever another Human enters" → trigger.event="creature-enters", scope.subtype="Human", scope.is_another=true
- Include keyword abilities as type="keyword" with effect.action="grant-keyword"
- Omit empty/null fields
- Return a JSON object: {{"cards": [{{...}}, {{...}}]}} wrapping all card results"""


def build_system_prompt():
    return f"""You are a Magic: The Gathering rules parser.
{MECHANICS_SCHEMA}"""


def build_user_prompt(cards: list[dict]) -> str:
    lines = []
    for c in cards:
        oracle = (c.get("oracle_text") or "")[:300]
        lines.append(f"CARD: {c['name']} [{c.get('type_line', '')}] CMC={c.get('cmc', 0)}")
        lines.append(f"Oracle: {oracle}")
        lines.append("")
    return "\n".join(lines)


# ── Card selection ────────────────────────────────────────────────────────

def get_target_cards(conn, mode: str, max_rank: int = 15000) -> list[dict]:
    """Get cards to process based on mode."""
    if mode == "mechanics-only":
        # Cards that have tags but NOT mechanics, rank <= max_rank, oracle > 30 chars
        rows = conn.execute("""
            SELECT c.oracle_id, c.name, c.type_line, c.mana_cost, c.cmc,
                   c.oracle_text, c.keywords, c.edhrec_rank
            FROM cards c
            WHERE c.legal_commander = 1
            AND c.oracle_id NOT IN (SELECT DISTINCT oracle_id FROM card_mechanics)
            AND c.oracle_text IS NOT NULL AND LENGTH(c.oracle_text) > 30
            AND c.edhrec_rank IS NOT NULL AND c.edhrec_rank <= ?
            ORDER BY c.edhrec_rank
        """, (max_rank,)).fetchall()
    elif mode == "combined":
        # All cards within rank cutoff with meaningful oracle text
        rows = conn.execute("""
            SELECT c.oracle_id, c.name, c.type_line, c.mana_cost, c.cmc,
                   c.oracle_text, c.keywords, c.edhrec_rank
            FROM cards c
            WHERE c.legal_commander = 1
            AND c.oracle_text IS NOT NULL AND LENGTH(c.oracle_text) > 30
            AND c.edhrec_rank IS NOT NULL AND c.edhrec_rank <= ?
            ORDER BY c.edhrec_rank
        """, (max_rank,)).fetchall()
    else:
        raise ValueError(f"Unknown mode: {mode}")

    return [{
        "oracle_id": r[0], "name": r[1], "type_line": r[2] or "",
        "mana_cost": r[3] or "", "cmc": r[4] or 0,
        "oracle_text": r[5] or "", "keywords": json.loads(r[6] or "[]"),
        "edhrec_rank": r[7],
    } for r in rows]


# ── Response parsing ──────────────────────────────────────────────────────

def parse_response(raw: str, expected_count: int) -> list[dict] | None:
    """Parse LLM response into list of card dicts with mechanics."""
    if not raw:
        return None

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
                return None
        else:
            return None

    if isinstance(result, dict):
        # Handle {"cards": [...]} wrapper from json_object mode
        for key in ("cards", "results", "data"):
            if key in result and isinstance(result[key], list):
                result = result[key]
                break
        else:
            result = [result]

    if not isinstance(result, list):
        return None

    if len(result) != expected_count:
        print(f"  [WARN] Expected {expected_count}, got {len(result)}")

    return result


# ── Store results ─────────────────────────────────────────────────────────

def store_mechanics(conn, oracle_id: str, mechanics: list[dict]):
    """Store extracted mechanics for a card."""
    # Delete existing mechanics for this card (re-extraction)
    conn.execute("DELETE FROM card_mechanics WHERE oracle_id = ?", (oracle_id,))

    for idx, m in enumerate(mechanics):
        mtype = m.get("type", "")
        trigger = m.get("trigger", {}) or {}
        effect = m.get("effect", {}) or {}
        modifier = m.get("modifier", {}) or {}
        scope = m.get("scope", {}) or {}

        conn.execute(
            "INSERT OR REPLACE INTO card_mechanics "
            "(oracle_id, mechanic_index, mechanic_type, trigger_event, trigger_filter, "
            "effect_action, effect_detail, modifier_target, modifier_how, scope_filter, cost, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                oracle_id, idx, mtype,
                trigger.get("event"),
                json.dumps(trigger.get("filter")) if trigger.get("filter") else None,
                effect.get("action"),
                json.dumps({k: v for k, v in effect.items() if k != "action"}) if effect else None,
                modifier.get("modifies"),
                modifier.get("how"),
                json.dumps(scope) if scope else None,
                m.get("cost"),
                json.dumps(m),
            )
        )


# ── Batch API ─────────────────────────────────────────────────────────────

def run_batch_api(cards: list[dict], api_key: str, model: str, batch_size: int = 50):
    """Submit cards via OpenAI Batch API and import results."""
    conn = sqlite3.connect(DB_PATH)

    # Ensure schema
    from extract_mechanics import ensure_schema
    ensure_schema(conn)

    system = build_system_prompt()

    # Build batches
    batches = []
    for i in range(0, len(cards), batch_size):
        batches.append(cards[i:i + batch_size])

    # Build JSONL
    jsonl_lines = []
    batch_map = {}
    token_param = "max_completion_tokens" if "gpt-5" in model else "max_tokens"
    for batch_idx, batch in enumerate(batches):
        custom_id = f"extract-{batch_idx}"
        user_prompt = build_user_prompt(batch)
        request = {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": model,
                token_param: 16384,
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

    jsonl_path = os.path.join(DATA_DIR, "batch_extract.jsonl")
    with open(jsonl_path, "w") as f:
        f.write("\n".join(jsonl_lines))
    print(f"  Created {jsonl_path} ({len(jsonl_lines)} requests, {len(cards)} cards)")

    # Upload
    print("  Uploading to OpenAI...")
    boundary = "----ExtractBoundary"
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
        "metadata": {"task": "mechanics-extraction"},
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
        return

    # Download results
    output_file_id = status_obj.get("output_file_id")
    if not output_file_id:
        print("  No output file!")
        return

    req = urllib.request.Request(
        f"https://api.openai.com/v1/files/{output_file_id}/content",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        output_data = resp.read().decode()

    # Parse and import
    total_cards = 0
    total_mechanics = 0
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

        parsed = parse_response(raw, len(batch))
        if not parsed:
            total_failed += len(batch)
            continue

        result_by_name = {r["name"].lower(): r for r in parsed if "name" in r}
        for card in batch:
            r = result_by_name.get(card["name"].lower())
            if r and r.get("mechanics"):
                store_mechanics(conn, card["oracle_id"], r["mechanics"])
                total_mechanics += len(r["mechanics"])
                total_cards += 1
            else:
                total_failed += 1

    conn.commit()
    conn.close()

    print(f"  Done: {total_cards} cards, {total_mechanics} mechanics extracted, {total_failed} failed")

    # Cleanup
    try:
        os.remove(jsonl_path)
    except OSError:
        pass


# ── Regular API (for --test mode) ─────────────────────────────────────────

def run_regular(cards: list[dict], api_key: str, model: str, batch_size: int = 50):
    """Score cards using regular API (for small tests)."""
    conn = sqlite3.connect(DB_PATH)
    from extract_mechanics import ensure_schema
    ensure_schema(conn)

    system = build_system_prompt()
    total_cards = 0
    total_mechanics = 0
    token_param = "max_completion_tokens" if "gpt-5" in model else "max_tokens"

    for i in range(0, len(cards), batch_size):
        batch = cards[i:i + batch_size]
        user = build_user_prompt(batch)

        payload = json.dumps({
            "model": model,
            token_param: 16384,
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
            print(f"  [ERROR] Batch {i//batch_size + 1}: {e}")
            continue

        parsed = parse_response(raw, len(batch))
        if not parsed:
            print(f"  [ERROR] Batch {i//batch_size + 1}: parse failed")
            continue

        result_by_name = {r["name"].lower(): r for r in parsed if "name" in r}
        for card in batch:
            r = result_by_name.get(card["name"].lower())
            if r and r.get("mechanics"):
                store_mechanics(conn, card["oracle_id"], r["mechanics"])
                total_mechanics += len(r["mechanics"])
                total_cards += 1

        conn.commit()
        print(f"  Batch {i//batch_size + 1}/{(len(cards) + batch_size - 1)//batch_size}: "
              f"{total_cards} cards, {total_mechanics} mechanics")

    conn.close()
    print(f"\n  Done: {total_cards} cards, {total_mechanics} mechanics")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Combined batch extraction: tags + mechanics")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mechanics-only", action="store_true",
                       help="Extract mechanics for cards that have tags but not mechanics")
    group.add_argument("--combined", action="store_true",
                       help="Retag + extract mechanics for all target cards")
    parser.add_argument("--max-rank", type=int, default=15000,
                        help="Max EDHREC rank to include (default: 15000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be processed without calling API")
    parser.add_argument("--test", type=int, default=0,
                        help="Test with N cards using regular API (not batch)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only first N cards (by EDHREC rank)")
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Cards per API request (default: 50)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key and not args.dry_run:
        print("ERROR: Set OPENAI_API_KEY environment variable")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    mode = "mechanics-only" if args.mechanics_only else "combined"
    cards = get_target_cards(conn, mode, args.max_rank)
    conn.close()

    if args.limit > 0:
        cards = cards[:args.limit]

    # Cost estimate
    batches = (len(cards) + args.batch_size - 1) // args.batch_size
    input_tokens = len(cards) * 100
    output_tokens = len(cards) * 200
    cost_regular = (input_tokens * 0.75 + output_tokens * 3.00) / 1_000_000
    cost_batch = cost_regular * 0.5

    print(f"Mode: {mode}")
    print(f"Target cards: {len(cards)} (EDHREC rank <= {args.max_rank})")
    print(f"Batches: {batches} × {args.batch_size} cards")
    print(f"Estimated cost: ${cost_regular:.2f} (regular) / ${cost_batch:.2f} (batch API)")

    if args.dry_run:
        print(f"\n[DRY RUN] First 10 cards:")
        for c in cards[:10]:
            print(f"  {c['name']:<40} rank={c['edhrec_rank']}")
        if len(cards) > 10:
            print(f"  ... and {len(cards) - 10} more")
        return

    if args.test > 0:
        test_cards = cards[:args.test]
        print(f"\n  Testing with {len(test_cards)} cards (regular API)...")
        run_regular(test_cards, api_key, args.model, args.batch_size)
    else:
        print(f"\n  Submitting to Batch API...")
        run_batch_api(cards, api_key, args.model, args.batch_size)


if __name__ == "__main__":
    main()
