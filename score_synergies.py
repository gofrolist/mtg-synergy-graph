#!/usr/bin/env python3
"""Score card synergies with commanders using LLM evaluation.

Scores every color-legal card against a commander on a 1-10 scale.
Results are cached in SQLite — only unscored cards are sent to the LLM.

Usage:
    # Score all cards for one commander
    python3 score_synergies.py --commander "Krenko, Mob Boss"

    # Score new cards against all known commanders
    python3 score_synergies.py --new-cards data/new_set.json

    # Score all cards for all deck commanders
    python3 score_synergies.py --all-decks

    # Dry run: show what would be scored + cost estimate
    python3 score_synergies.py --commander "Krenko, Mob Boss" --dry-run

    # Use a different model/provider
    python3 score_synergies.py --commander "Krenko, Mob Boss" --provider ollama --model mtg-tagger

    # Show scoring stats
    python3 score_synergies.py --stats
"""

import argparse
import json
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tags.db")
BATCH_SIZE = 200  # Cards per API call (~8k tokens input, well within 1M context)


# ── Schema ──────────────────────────────────────────────────────────────────

def ensure_schema(conn: sqlite3.Connection):
    """Create synergy_scores table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS synergy_scores (
            commander_oid TEXT NOT NULL,
            card_oid TEXT NOT NULL,
            score INTEGER NOT NULL,      -- 1-10
            reasoning TEXT,              -- brief explanation
            model TEXT,                  -- model used for scoring
            scored_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (commander_oid, card_oid)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_synergy_scores_commander
        ON synergy_scores(commander_oid)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_synergy_scores_card
        ON synergy_scores(card_oid)
    """)
    conn.commit()


# ── Card loading ────────────────────────────────────────────────────────────

def load_commander(conn: sqlite3.Connection, name: str) -> dict:
    """Load commander data from DB."""
    row = conn.execute(
        "SELECT oracle_id, name, oracle_text, type_line, color_identity, keywords, mana_cost "
        "FROM cards WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        raise ValueError(f"Commander not found: {name}")
    return {
        "oracle_id": row[0],
        "name": row[1],
        "oracle_text": row[2] or "",
        "type_line": row[3] or "",
        "color_identity": set(json.loads(row[4] or "[]")),
        "keywords": json.loads(row[5] or "[]"),
        "mana_cost": row[6] or "",
    }


def get_color_legal_cards(conn: sqlite3.Connection, color_identity: set) -> list[dict]:
    """Get all commander-legal cards within color identity."""
    rows = conn.execute(
        "SELECT oracle_id, name, oracle_text, type_line, mana_cost, cmc, keywords, color_identity, edhrec_rank "
        "FROM cards WHERE legal_commander = 1"
    ).fetchall()

    cards = []
    for r in rows:
        card_ci = set(json.loads(r[7] or "[]"))
        if card_ci.issubset(color_identity):
            cards.append({
                "oracle_id": r[0],
                "name": r[1],
                "oracle_text": r[2] or "",
                "type_line": r[3] or "",
                "mana_cost": r[4] or "",
                "cmc": r[5] or 0,
                "keywords": json.loads(r[6] or "[]"),
                "edhrec_rank": r[8],
            })
    return cards


def get_already_scored(conn: sqlite3.Connection, commander_oid: str) -> set:
    """Get oracle_ids already scored for this commander."""
    rows = conn.execute(
        "SELECT card_oid FROM synergy_scores WHERE commander_oid = ?",
        (commander_oid,)
    ).fetchall()
    return {r[0] for r in rows}


# ── Prompt building ─────────────────────────────────────────────────────────

def build_system_prompt(commander: dict) -> str:
    """Build system prompt for synergy scoring."""
    strategies_row = None
    try:
        conn = sqlite3.connect(DB_PATH)
        strategies = [r[0] for r in conn.execute(
            "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.5",
            (commander["oracle_id"],)
        ).fetchall()]
        conn.close()
    except Exception:
        strategies = []

    strategy_text = f"Commander strategies: {', '.join(strategies)}" if strategies else ""

    return f"""You are an expert Magic: The Gathering EDH/Commander deckbuilder.

Your task: rate how synergistic each card is with the given commander on a scale of 1-10.

Commander: {commander["name"]}
Type: {commander["type_line"]}
Mana cost: {commander["mana_cost"]}
Oracle text: {commander["oracle_text"]}
Keywords: {', '.join(commander["keywords"]) if commander["keywords"] else 'none'}
{strategy_text}

Scoring guide:
- 10: Essential combo piece or perfect synergy. The card is built for this commander.
- 8-9: Strong synergy. Directly enables or benefits from the commander's abilities.
- 6-7: Good synergy. Fits the deck's strategy and works well with the commander.
- 4-5: Moderate. Generically useful but not specifically synergistic with this commander.
- 2-3: Weak. Technically legal but doesn't advance the commander's game plan.
- 1: No synergy or anti-synergy. Actively bad in this deck.

Consider:
- Does the card directly interact with the commander's abilities?
- Does it enable the commander's strategy (e.g., tokens for token commanders)?
- Does it benefit from what the commander provides?
- Is it a key piece for the archetype this commander supports?
- Would an experienced EDH player specifically choose this for THIS commander?
- COMBO CHECK: Does this card + commander create an infinite or near-infinite loop? If yes, score 9-10.
- UNTAP CHECK: Does this card untap the commander or let you activate it again? If the commander has a tap ability, untap effects score 7-9.
- TRIGGER DOUBLING: If the commander triggers on attacks/ETBs/deaths, cards that double those triggers score 8-9.
- TRIBAL: If the commander cares about a creature type, creatures of that type score 6-8 just for being the right type.

A card that's generically good (Sol Ring, Swords to Plowshares) but not specifically synergistic should score 4-5, not higher.
A card that's mediocre in general but amazing with THIS commander should score 8+.

Return a JSON array with objects: {{"name": "Card Name", "score": N, "reason": "brief explanation"}}
Return ONLY the JSON array, no other text."""


def build_user_prompt(cards: list[dict]) -> str:
    """Build user prompt with card batch."""
    lines = ["Rate synergy for each card:\n"]
    for i, c in enumerate(cards, 1):
        oracle = c["oracle_text"]
        # Truncate very long oracle text
        if len(oracle) > 300:
            oracle = oracle[:297] + "..."
        lines.append(f"CARD {i}:")
        lines.append(f"Name: {c['name']}")
        lines.append(f"Type: {c['type_line']}")
        lines.append(f"CMC: {c['cmc']}")
        if c["keywords"]:
            lines.append(f"Keywords: {', '.join(c['keywords'])}")
        lines.append(f"Oracle: {oracle}")
        lines.append("")
    return "\n".join(lines)


# ── API call ────────────────────────────────────────────────────────────────

def call_api(system: str, user: str, api_key: str,
             provider: str = "openai", model: str = "gpt-5.4-mini") -> str | None:
    """Call LLM API with robust retry and rate limit handling."""
    max_attempts = 6  # More attempts with longer backoff for 429s

    for attempt in range(max_attempts):
        try:
            if provider == "openai":
                token_param = "max_completion_tokens" if "gpt-5" in model else "max_tokens"
                payload = {
                    "model": model,
                    token_param: 4096,
                    "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                }
                req = urllib.request.Request(
                    "https://api.openai.com/v1/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                )
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read())
                    return data["choices"][0]["message"]["content"].strip()

            elif provider == "ollama":
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "options": {"temperature": 0.2, "num_predict": 4096},
                }
                # Disable thinking for models that support it (qwen3)
                if "qwen" in model.lower():
                    payload["think"] = False
                req = urllib.request.Request(
                    "http://localhost:11434/api/chat",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read())
                    return data["message"]["content"].strip()

            elif provider == "anthropic":
                payload = {
                    "model": model,
                    "max_tokens": 4096,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                }
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=json.dumps(payload).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read())
                    return data["content"][0]["text"].strip()

        except urllib.request.HTTPError as e:
            if e.code == 429:
                # Rate limited — use Retry-After header or exponential backoff
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    wait = float(retry_after)
                else:
                    wait = min(2 ** (attempt + 1), 60)  # 2, 4, 8, 16, 32, 60
                time.sleep(wait)
                continue
            elif e.code >= 500:
                # Server error — retry with backoff
                time.sleep(2 ** attempt)
                continue
            else:
                # Client error (400, etc.) — read body for details
                body = e.read().decode()[:200]
                print(f"  [API ERROR {e.code}] {body}")
                return None
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, OSError) as e:
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  [API ERROR] {e}")
        except Exception as e:
            # Non-retryable error — fail immediately
            print(f"  [API ERROR] {type(e).__name__}: {e}")
            return None

    return None


def parse_response(raw: str, expected_count: int) -> list[dict] | None:
    """Parse LLM response into list of {name, score, reason}."""
    if not raw:
        return None

    # Strip thinking tags, markdown fences
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from mixed text
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError as e:
                print(f"  [JSON ERROR] {e}")
                print(f"  Raw (first 300 chars): {raw[:300]}")
                return None
        else:
            print(f"  [JSON ERROR] No JSON array found")
            print(f"  Raw (first 300 chars): {raw[:300]}")
            return None

    if isinstance(result, dict):
        result = [result]

    if len(result) != expected_count:
        print(f"  [WARN] Expected {expected_count} cards, got {len(result)}")

    # Validate each entry
    valid = []
    for entry in result:
        name = entry.get("name", "")
        score = entry.get("score", 0)
        reason = entry.get("reason", "")
        if isinstance(score, (int, float)) and 1 <= score <= 10:
            valid.append({"name": name, "score": int(round(score)), "reason": reason})
        else:
            print(f"  [WARN] Invalid score for {name}: {score}")

    return valid


# ── OpenAI Batch API ────────────────────────────────────────────────────────

def _score_via_batch_api(commander: dict, batches: list, system_prompt: str,
                         api_key: str, model: str, conn: sqlite3.Connection):
    """Score using OpenAI Batch API: upload JSONL, poll, import results. 50% cheaper."""
    import tempfile

    # 1. Build JSONL request file
    jsonl_lines = []
    batch_map = {}  # custom_id → batch (list of cards)
    for batch_idx, batch in enumerate(batches):
        custom_id = f"batch-{batch_idx}"
        user_prompt = build_user_prompt(batch)
        token_param = "max_completion_tokens" if "gpt-5" in model else "max_tokens"
        request = {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": model,
                token_param: 4096,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
        }
        jsonl_lines.append(json.dumps(request))
        batch_map[custom_id] = batch

    # Write JSONL to temp file
    jsonl_path = os.path.join("data", f"batch_{commander['name'].lower().replace(' ', '_')}.jsonl")
    with open(jsonl_path, "w") as f:
        f.write("\n".join(jsonl_lines))
    print(f"  Created batch file: {jsonl_path} ({len(jsonl_lines)} requests)")

    # 2. Upload file
    print("  Uploading to OpenAI...")
    import urllib.request as urlreq

    # Upload file via multipart form
    boundary = "----BatchBoundary"
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

    req = urlreq.Request(
        "https://api.openai.com/v1/files",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urlreq.urlopen(req, timeout=60) as resp:
        file_obj = json.loads(resp.read())
    file_id = file_obj["id"]
    print(f"  File uploaded: {file_id}")

    # 3. Create batch
    batch_payload = json.dumps({
        "input_file_id": file_id,
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
        "metadata": {"commander": commander["name"]},
    }).encode()

    req = urlreq.Request(
        "https://api.openai.com/v1/batches",
        data=batch_payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlreq.urlopen(req, timeout=60) as resp:
            batch_obj = json.loads(resp.read())
    except urlreq.HTTPError as e:
        body = e.read().decode()[:500]
        print(f"  Batch creation failed: {e.code} {body}")
        return
    batch_id = batch_obj["id"]
    print(f"  Batch created: {batch_id}")

    # 4. Poll for completion
    print("  Waiting for completion...", end="", flush=True)
    while True:
        time.sleep(10)
        req = urlreq.Request(
            f"https://api.openai.com/v1/batches/{batch_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urlreq.urlopen(req, timeout=30) as resp:
            status_obj = json.loads(resp.read())

        status = status_obj["status"]
        completed = status_obj.get("request_counts", {}).get("completed", 0)
        total = status_obj.get("request_counts", {}).get("total", len(batches))
        print(f"\r  Status: {status} ({completed}/{total})", end="", flush=True)

        if status in ("completed", "failed", "expired", "cancelled"):
            print()
            break

    if status != "completed":
        print(f"  Batch {status}! Errors: {status_obj.get('errors', {})}")
        return

    # 5. Download results
    output_file_id = status_obj.get("output_file_id")
    if not output_file_id:
        print("  No output file!")
        return

    req = urlreq.Request(
        f"https://api.openai.com/v1/files/{output_file_id}/content",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urlreq.urlopen(req, timeout=120) as resp:
        output_data = resp.read().decode()

    # 6. Parse and import results
    total_scored = 0
    total_failed = 0
    for line in output_data.strip().split("\n"):
        result = json.loads(line)
        custom_id = result["custom_id"]
        batch = batch_map.get(custom_id, [])

        response = result.get("response", {})
        if response.get("status_code") != 200:
            total_failed += len(batch)
            continue

        raw = response["body"]["choices"][0]["message"]["content"].strip()
        parsed = parse_response(raw, len(batch))
        if not parsed:
            total_failed += len(batch)
            continue

        result_by_name = {r["name"].lower(): r for r in parsed}
        for card in batch:
            r = result_by_name.get(card["name"].lower())
            if r:
                conn.execute(
                    "INSERT OR REPLACE INTO synergy_scores "
                    "(commander_oid, card_oid, score, reasoning, model) VALUES (?, ?, ?, ?, ?)",
                    (commander["oracle_id"], card["oracle_id"],
                     r["score"], r["reason"], f"openai-batch/{model}")
                )
                total_scored += 1
            else:
                total_failed += 1

    conn.commit()
    print(f"  Done: {total_scored} scored, {total_failed} failed")

    # Cleanup temp file
    os.remove(jsonl_path)


# ── Main scoring logic ──────────────────────────────────────────────────────

def score_commander(commander_name: str, provider: str, model: str,
                    api_key: str, dry_run: bool = False,
                    card_filter: list[str] | None = None,
                    use_batch_api: bool = False):
    """Score all color-legal cards against a commander."""
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)

    commander = load_commander(conn, commander_name)
    print(f"Commander: {commander['name']} ({','.join(sorted(commander['color_identity']))})")

    # Get candidates
    if card_filter:
        # Only score specific cards (e.g., new set)
        placeholders = ",".join("?" * len(card_filter))
        rows = conn.execute(
            f"SELECT oracle_id, name, oracle_text, type_line, mana_cost, cmc, keywords "
            f"FROM cards WHERE name IN ({placeholders}) AND legal_commander = 1",
            card_filter
        ).fetchall()
        candidates = [{
            "oracle_id": r[0], "name": r[1], "oracle_text": r[2] or "",
            "type_line": r[3] or "", "mana_cost": r[4] or "", "cmc": r[5] or 0,
            "keywords": json.loads(r[6] or "[]"),
        } for r in rows]
    else:
        candidates = get_color_legal_cards(conn, commander["color_identity"])

    # Filter already scored
    scored = get_already_scored(conn, commander["oracle_id"])
    unscored = [c for c in candidates if c["oracle_id"] not in scored]

    # Exclude commander itself
    unscored = [c for c in unscored if c["oracle_id"] != commander["oracle_id"]]

    # Smart pre-filter: auto-score cards with zero synergy signals.
    # Cards that share NO tags, NO creature types, NO keywords, and NO
    # oracle text concepts with the commander are almost certainly score 1-3.
    # Auto-assign them score 2 and only send promising cards to the LLM.
    cmdr_provides = set()
    cmdr_wants = set()
    cmdr_keywords = set()
    cmdr_subtypes = set()
    cmdr_oracle_lower = (commander.get("oracle_text") or "").lower()

    for r in conn.execute("SELECT tag FROM provides WHERE oracle_id = ?",
                          (commander["oracle_id"],)):
        cmdr_provides.add(r[0])
    for r in conn.execute("SELECT tag FROM wants WHERE oracle_id = ?",
                          (commander["oracle_id"],)):
        cmdr_wants.add(r[0])
    cmdr_keywords = {k.lower() for k in commander.get("keywords", [])}
    cmdr_type_line = (commander.get("type_line") or "").lower()
    if " — " in cmdr_type_line:
        cmdr_subtypes = {s.strip().lower() for s in cmdr_type_line.split(" — ")[1].split()}

    # Concepts the commander cares about
    cmdr_concepts = set()
    for word in ["human", "goblin", "elf", "zombie", "vampire", "dragon", "angel",
                 "demon", "artifact", "enchantment", "equipment", "aura",
                 "instant", "sorcery", "token", "counter", "poison",
                 "mill", "draw", "discard", "sacrifice", "exile",
                 "graveyard", "damage", "life", "proliferate", "attack",
                 "enters", "dies", "cast", "creature"]:
        if word in cmdr_oracle_lower:
            cmdr_concepts.add(word)

    # Batch-load tags for unscored cards using temp table (avoids huge IN clause)
    unscored_oids = {c["oracle_id"] for c in unscored}
    card_provides = {}
    card_wants = {}
    if unscored_oids:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _unscored_oids (oid TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _unscored_oids")
        conn.executemany("INSERT INTO _unscored_oids VALUES (?)",
                         [(oid,) for oid in unscored_oids])
        for oid, tag in conn.execute(
            "SELECT p.oracle_id, p.tag FROM provides p "
            "JOIN _unscored_oids u ON p.oracle_id = u.oid"
        ):
            card_provides.setdefault(oid, set()).add(tag)
        for oid, tag in conn.execute(
            "SELECT w.oracle_id, w.tag FROM wants w "
            "JOIN _unscored_oids u ON w.oracle_id = u.oid"
        ):
            card_wants.setdefault(oid, set()).add(tag)
        conn.execute("DROP TABLE IF EXISTS _unscored_oids")

    # Split into LLM-worthy vs auto-score
    llm_candidates = []
    auto_scored = 0
    for c in unscored:
        oid = c["oracle_id"]
        cp = card_provides.get(oid, set())
        cw = card_wants.get(oid, set())
        card_kw = {k.lower() for k in c.get("keywords", [])}
        card_type = (c.get("type_line") or "").lower()
        card_oracle = (c.get("oracle_text") or "").lower()
        card_oracle_clean = re.sub(r'\([^)]*\)', '', card_oracle)

        # Check for ANY synergy signal
        has_tag_overlap = bool(
            (cp & cmdr_wants) or (cw & cmdr_provides) or
            (cp & cmdr_provides) or (cw & cmdr_wants)
        )
        has_keyword_overlap = bool(card_kw & cmdr_keywords)
        has_type_overlap = False
        if cmdr_subtypes:
            for st in cmdr_subtypes:
                if len(st) > 3 and (st in card_type or st in card_oracle_clean):
                    has_type_overlap = True
                    break
        has_concept_overlap = False
        if cmdr_concepts and card_oracle_clean:
            has_concept_overlap = any(c in card_oracle_clean for c in cmdr_concepts)

        # Cards with at least one signal → send to LLM
        # Cards with zero signals → auto-score 2
        if has_tag_overlap or has_keyword_overlap or has_type_overlap or has_concept_overlap:
            llm_candidates.append(c)
        else:
            conn.execute(
                "INSERT OR REPLACE INTO synergy_scores "
                "(commander_oid, card_oid, score, reasoning, model) VALUES (?, ?, ?, ?, ?)",
                (commander["oracle_id"], oid, 2, "No synergy signals detected", "auto-filter")
            )
            auto_scored += 1

    conn.commit()
    unscored = llm_candidates

    # For local models (ollama): limit to top candidates by pre-ranking.
    # Local models are slower, so scoring 15k cards isn't practical.
    # Pre-rank by: EDHREC rank (card quality), mechanics score, tag overlap count.
    MAX_LOCAL_CANDIDATES = 2000
    local_trimmed = 0
    if len(unscored) > MAX_LOCAL_CANDIDATES:
        # Score each candidate cheaply for pre-ranking
        try:
            from mechanics_matcher import load_mechanics, compute_synergy
            _all_mechs = load_mechanics(DB_PATH)
            cmdr_mechs_pre = _all_mechs.get(commander["oracle_id"], [])
        except Exception:
            _all_mechs = {}
            cmdr_mechs_pre = []

        def _pre_score(card):
            oid = card["oracle_id"]
            score = 0.0
            # Tag overlap (already computed above)
            cp = card_provides.get(oid, set())
            cw = card_wants.get(oid, set())
            score += len(cp & cmdr_wants) * 2 + len(cw & cmdr_provides) * 2
            # Type overlap with commander
            card_type = (card.get("type_line") or "").lower()
            for st in cmdr_subtypes:
                if len(st) > 3 and st in card_type:
                    score += 5
            # Concept overlap
            card_oracle_c = (card.get("oracle_text") or "").lower()
            score += sum(1 for c in cmdr_concepts if c in card_oracle_c)
            # Mechanics score
            if cmdr_mechs_pre:
                cm = _all_mechs.get(oid, [])
                if cm:
                    score += compute_synergy(cmdr_mechs_pre, cm,
                                             commander.get("type_line", ""),
                                             card.get("type_line", ""))
            # EDHREC rank bonus (lower rank = better card)
            rank = card.get("edhrec_rank") or 50000
            if rank < 1000:
                score += 3
            elif rank < 5000:
                score += 1
            return score

        unscored.sort(key=lambda c: -_pre_score(c))
        local_trimmed = len(unscored) - MAX_LOCAL_CANDIDATES
        # Auto-score the rest as 3 (below average but with some signal)
        for c in unscored[MAX_LOCAL_CANDIDATES:]:
            conn.execute(
                "INSERT OR REPLACE INTO synergy_scores "
                "(commander_oid, card_oid, score, reasoning, model) VALUES (?, ?, ?, ?, ?)",
                (commander["oracle_id"], c["oracle_id"], 3, "Pre-ranked below threshold", "auto-prerank")
            )
        conn.commit()
        unscored = unscored[:MAX_LOCAL_CANDIDATES]

    print(f"  Total color-legal: {len(candidates)}")
    print(f"  Already scored: {len(scored)}")
    print(f"  Auto-scored (no signals): {auto_scored}")
    if local_trimmed:
        print(f"  Auto-scored (pre-ranked low): {local_trimmed}")
    print(f"  To score via LLM: {len(unscored)}")

    if not unscored:
        print("  Nothing to score!")
        conn.close()
        return

    # Batch — smaller for local models (ollama context is limited)
    batch_size = 10 if provider == "ollama" else BATCH_SIZE
    batches = []
    for i in range(0, len(unscored), batch_size):
        batches.append(unscored[i:i + batch_size])

    # Cost estimate
    system_prompt = build_system_prompt(commander)
    avg_user_tokens = 150 * BATCH_SIZE  # ~150 tokens per card
    avg_output_tokens = 30 * BATCH_SIZE  # ~30 tokens per card response
    total_input = (len(system_prompt) // 4 + avg_user_tokens) * len(batches)
    total_output = avg_output_tokens * len(batches)

    # Pricing per 1M tokens (standard / batch)
    pricing = {
        "gpt-4.1-mini": (0.40, 1.60, 0.20, 0.80),
        "gpt-5.4-mini": (0.75, 4.50, 0.375, 2.25),
    }
    if provider == "openai":
        p = pricing.get(model, (0.75, 4.50, 0.375, 2.25))  # default to gpt-5.4-mini
        cost = total_input * p[0] / 1_000_000 + total_output * p[1] / 1_000_000
        batch_cost = total_input * p[2] / 1_000_000 + total_output * p[3] / 1_000_000
    elif provider == "anthropic":
        cost = total_input * 3.0 / 1_000_000 + total_output * 15.0 / 1_000_000
        batch_cost = cost
    else:
        cost = 0.0
        batch_cost = 0.0

    print(f"  Batches: {len(batches)} × {batch_size} cards")
    print(f"  Estimated cost: ${cost:.2f} (${batch_cost:.2f} with --batch-api)")
    if provider == "ollama":
        # Local model: sequential, ~1.5s per card
        est_time = len(unscored) * 1.5 / 60
        print(f"  Estimated time: {est_time:.0f} min (local, sequential)")
    else:
        concurrency = 5
        est_time = len(batches) * 3.0 / concurrency / 60
        print(f"  Estimated time: {est_time:.1f} min ({concurrency} concurrent)")

    if dry_run:
        print("\n  [DRY RUN] Would score these cards:")
        for c in unscored[:10]:
            print(f"    - {c['name']}")
        if len(unscored) > 10:
            print(f"    ... and {len(unscored) - 10} more")
        conn.close()
        return

    # OpenAI Batch API mode: upload JSONL, poll for results, 50% cheaper
    if use_batch_api and provider == "openai":
        _score_via_batch_api(commander, batches, system_prompt, api_key, model, conn)
        conn.close()
        return

    # Score batches — concurrent with rate limiting
    total_scored = 0
    total_failed = 0
    start = time.time()
    max_workers = 1 if provider == "ollama" else 5  # Conservative to avoid 429s

    import threading
    rate_lock = threading.Lock()
    last_request_time = [0.0]  # Mutable for closure
    min_interval = 0.3 if provider != "ollama" else 0  # Min seconds between requests

    def _process_batch(batch_idx_and_batch):
        batch_idx, batch = batch_idx_and_batch
        # Rate limit: ensure min interval between request starts
        with rate_lock:
            now = time.time()
            wait = max(0, min_interval - (now - last_request_time[0]))
            if wait > 0:
                time.sleep(wait)
            last_request_time[0] = time.time()

        user_prompt = build_user_prompt(batch)
        raw = call_api(system_prompt, user_prompt, api_key, provider, model)
        results = parse_response(raw, len(batch))
        return batch_idx, batch, results

    from concurrent.futures import ThreadPoolExecutor, as_completed
    pending = list(enumerate(batches))
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process_batch, item): item for item in pending}

        for future in as_completed(futures):
            batch_idx, batch, results = future.result()
            completed += 1

            if not results:
                total_failed += len(batch)
                print(f"  Batch {batch_idx + 1}/{len(batches)} FAILED  [{completed}/{len(batches)}]")
                continue

            result_by_name = {r["name"].lower(): r for r in results}
            matched = 0
            for card in batch:
                r = result_by_name.get(card["name"].lower())
                if r:
                    conn.execute(
                        "INSERT OR REPLACE INTO synergy_scores "
                        "(commander_oid, card_oid, score, reasoning, model) VALUES (?, ?, ?, ?, ?)",
                        (commander["oracle_id"], card["oracle_id"],
                         r["score"], r["reason"], f"{provider}/{model}")
                    )
                    matched += 1

            conn.commit()
            total_scored += matched
            total_failed += len(batch) - matched
            if completed % 10 == 0 or completed == len(batches):
                elapsed_so_far = time.time() - start
                rate = completed / elapsed_so_far * 60
                print(f"  Progress: {completed}/{len(batches)} batches, "
                      f"{total_scored} scored, {rate:.0f} batches/min")

    elapsed = time.time() - start
    print(f"\n  Done: {total_scored} scored, {total_failed} failed, {elapsed:.1f}s")

    # Auto-retry failed cards (one pass)
    if total_failed > 0:
        scored_now = get_already_scored(conn, commander["oracle_id"])
        still_unscored = [c for c in unscored if c["oracle_id"] not in scored_now]
        if still_unscored:
            print(f"  Retrying {len(still_unscored)} failed cards...")
            retry_batches = []
            for i in range(0, len(still_unscored), BATCH_SIZE):
                retry_batches.append(still_unscored[i:i + BATCH_SIZE])

            for batch in retry_batches:
                user_prompt = build_user_prompt(batch)
                time.sleep(1)  # Be gentle on retry
                raw = call_api(system_prompt, user_prompt, api_key, provider, model)
                results = parse_response(raw, len(batch))
                if results:
                    result_by_name = {r["name"].lower(): r for r in results}
                    for card in batch:
                        r = result_by_name.get(card["name"].lower())
                        if r:
                            conn.execute(
                                "INSERT OR REPLACE INTO synergy_scores "
                                "(commander_oid, card_oid, score, reasoning, model) VALUES (?, ?, ?, ?, ?)",
                                (commander["oracle_id"], card["oracle_id"],
                                 r["score"], r["reason"], f"{provider}/{model}")
                            )
                            total_scored += 1
                    conn.commit()

            final_scored = conn.execute(
                "SELECT COUNT(*) FROM synergy_scores WHERE commander_oid = ?",
                (commander["oracle_id"],)
            ).fetchone()[0]
            total_cards = len(unscored)
            print(f"  Final: {final_scored}/{total_cards} scored "
                  f"({final_scored/total_cards*100:.1f}% coverage)")

    conn.close()


def show_stats():
    """Show scoring statistics."""
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)

    total = conn.execute("SELECT COUNT(*) FROM synergy_scores").fetchone()[0]
    print(f"Total synergy scores: {total}")

    print("\nPer commander:")
    for row in conn.execute("""
        SELECT c.name, COUNT(*) as cnt,
               ROUND(AVG(ss.score), 1) as avg_score,
               ss.model
        FROM synergy_scores ss
        JOIN cards c ON c.oracle_id = ss.commander_oid
        GROUP BY ss.commander_oid
        ORDER BY cnt DESC
    """).fetchall():
        print(f"  {row[0]:<40} {row[1]:>5} cards  avg={row[2]}  model={row[3]}")

    print("\nScore distribution:")
    for row in conn.execute("""
        SELECT score, COUNT(*) as cnt
        FROM synergy_scores
        GROUP BY score
        ORDER BY score
    """).fetchall():
        bar = "█" * (row[1] // 100)
        print(f"  {row[0]:>2}: {row[1]:>6} {bar}")

    conn.close()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Score card synergies with commanders via LLM")
    parser.add_argument("--commander", help="Commander name to score against")
    parser.add_argument("--all-decks", action="store_true", help="Score for all deck commanders")
    parser.add_argument("--new-cards", help="JSON file with new card names to score against all commanders")
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic", "ollama"])
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--dry-run", action="store_true", help="Show cost estimate without scoring")
    parser.add_argument("--batch-api", action="store_true",
                        help="Use OpenAI Batch API (50%% cheaper, async processing)")
    parser.add_argument("--no-batch", action="store_true",
                        help="Disable auto Batch API for --all-decks")
    parser.add_argument("--stats", action="store_true", help="Show scoring statistics")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    # Get API key
    api_key = ""
    if args.provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key and not args.dry_run:
            print("ERROR: Set OPENAI_API_KEY environment variable")
            sys.exit(1)
    elif args.provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key and not args.dry_run:
            print("ERROR: Set ANTHROPIC_API_KEY environment variable")
            sys.exit(1)

    if args.new_cards:
        # Score new cards against all deck commanders
        new_cards = json.loads(Path(args.new_cards).read_text())
        if isinstance(new_cards, list) and isinstance(new_cards[0], dict):
            card_names = [c["name"] for c in new_cards]
        elif isinstance(new_cards, list):
            card_names = new_cards
        else:
            print("ERROR: new-cards file should be a JSON array of card names or objects")
            sys.exit(1)

        from decks import list_decks, load_deck
        for deck_name in list_decks():
            deck = load_deck(deck_name)
            print(f"\n{'='*60}")
            score_commander(deck.COMMANDER, args.provider, args.model,
                            api_key, args.dry_run, card_filter=card_names,
                            use_batch_api=args.batch_api)

    elif args.all_decks:
        # Default to Batch API for bulk scoring (50% cheaper, acceptable latency)
        use_batch = args.batch_api if args.batch_api else (args.provider == "openai" and not args.no_batch)
        if use_batch and not args.batch_api:
            print("Using Batch API for --all-decks (50% cheaper). Add --no-batch to disable.")
        from decks import list_decks, load_deck
        for deck_name in list_decks():
            deck = load_deck(deck_name)
            print(f"\n{'='*60}")
            score_commander(deck.COMMANDER, args.provider, args.model,
                            api_key, args.dry_run,
                            use_batch_api=use_batch)

    elif args.commander:
        score_commander(args.commander, args.provider, args.model,
                        api_key, args.dry_run,
                        use_batch_api=args.batch_api)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
