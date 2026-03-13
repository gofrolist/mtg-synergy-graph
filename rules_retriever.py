"""
Retrieve relevant MTG rules for card tagging via vector similarity search.

Loads the pre-built rules index and provides a query interface
for prompt_builder to inject relevant rules context.

Usage as module:
    from rules_retriever import retrieve_rules_for_cards
    context = retrieve_rules_for_cards(cards)

Usage as CLI (for testing):
    python3 rules_retriever.py "Flash"
    python3 rules_retriever.py "replacement effect +1/+1 counters"
"""

import json
import os
import sys

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INDEX_FILE = os.path.join(DATA_DIR, "rules_index.json")

# Lazy-loaded singleton
_INDEX = None

# Keyword → section lookup for guaranteed retrieval of key rules
KEYWORD_SECTIONS = {
    "deathtouch": "702.2",
    "defender": "702.3",
    "double strike": "702.4",
    "first strike": "702.7",
    "flash": "702.8",
    "flying": "702.9",
    "haste": "702.10",
    "hexproof": "702.11",
    "indestructible": "702.12",
    "lifelink": "702.15",
    "menace": "702.110",
    "protection": "702.16",
    "reach": "702.17",
    "trample": "702.19",
    "vigilance": "702.20",
    "ward": "702.21",
    "changeling": "702.73",
    "proliferate": "701.27",
    "populate": "701.30",
}


def _load_index() -> dict:
    global _INDEX
    if _INDEX is None:
        if not os.path.exists(INDEX_FILE):
            print(f"WARNING: Rules index not found at {INDEX_FILE}", file=sys.stderr)
            print(f"Run: python3 rules_fetcher.py && python3 rules_index.py", file=sys.stderr)
            _INDEX = {"chunks": [], "dimension": 0}
        else:
            with open(INDEX_FILE) as f:
                _INDEX = json.load(f)
    return _INDEX


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def search(query_embedding: list[float], top_k: int = 5) -> list[dict]:
    """Find top-K most similar chunks to query embedding."""
    index = _load_index()
    chunks = index["chunks"]

    if not chunks or not query_embedding:
        return []

    scored = []
    for chunk in chunks:
        emb = chunk.get("embedding", [])
        if not emb:
            continue
        sim = cosine_sim(query_embedding, emb)
        scored.append((sim, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"score": s, **c} for s, c in scored[:top_k]]


def search_by_section(section_prefix: str) -> list[dict]:
    """Find chunks matching a section number prefix."""
    index = _load_index()
    return [c for c in index["chunks"] if c["section"].startswith(section_prefix)]


def retrieve_rules_for_cards(
    cards: list[dict],
    top_k: int = 5,
    max_total: int = 15,
    max_chars: int = 4000,
) -> str:
    """Retrieve relevant rule chunks for a batch of cards.

    For each card:
    1. Embed oracle text → semantic search for relevant rules
    2. Look up keywords → guaranteed retrieval of keyword rules

    Returns formatted text block for prompt injection.
    """
    from rules_index import embed_query

    index = _load_index()
    if not index["chunks"]:
        return ""

    all_results = {}  # chunk_id -> (score, chunk)

    for card in cards:
        oracle_text = card.get("oracle_text", "")
        keywords = card.get("keywords", [])
        oracle_id = card.get("oracle_id", "")

        # Direct oracle_id match — get card-specific rulings/errata
        if oracle_id:
            ruling_chunks = [c for c in index["chunks"]
                             if c.get("section", "").startswith("ruling:")
                             and c.get("oracle_id") == oracle_id]
            for chunk in ruling_chunks:
                cid = chunk["id"]
                all_results[cid] = (1.5, {**chunk, "score": 1.5})  # highest priority

        # Semantic search on oracle text
        if oracle_text:
            query = f"Magic: The Gathering rules: {oracle_text}"
            query_emb = embed_query(query)
            results = search(query_emb, top_k=top_k)
            for r in results:
                cid = r["id"]
                if cid not in all_results or r["score"] > all_results[cid][0]:
                    all_results[cid] = (r["score"], r)

        # Keyword lookup (guaranteed retrieval)
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in KEYWORD_SECTIONS:
                section = KEYWORD_SECTIONS[kw_lower]
                section_chunks = search_by_section(section)
                for chunk in section_chunks:
                    cid = chunk["id"]
                    if cid not in all_results:
                        all_results[cid] = (1.0, {**chunk, "score": 1.0})

        # Also check oracle text for keyword mentions
        oracle_lower = oracle_text.lower()
        for kw, section in KEYWORD_SECTIONS.items():
            if kw in oracle_lower:
                section_chunks = search_by_section(section)
                for chunk in section_chunks:
                    cid = chunk["id"]
                    if cid not in all_results:
                        all_results[cid] = (0.9, {**chunk, "score": 0.9})

    # Sort by score, take top max_total
    sorted_results = sorted(all_results.values(), key=lambda x: x[0], reverse=True)
    top_results = [r for _, r in sorted_results[:max_total]]

    # Format output, respecting char budget
    lines = []
    total_chars = 0
    for r in top_results:
        section_line = f"[Rule {r['section']} — {r['title']}]"
        text = r["text"]

        # Truncate long chunks
        if len(text) > 600:
            text = text[:600] + "..."

        entry = f"{section_line}\n{text}"
        if total_chars + len(entry) > max_chars:
            break
        lines.append(entry)
        total_chars += len(entry)

    return "\n\n".join(lines)


if __name__ == "__main__":
    # CLI test mode
    if len(sys.argv) < 2:
        print("Usage: python3 rules_retriever.py <query>")
        print("Example: python3 rules_retriever.py 'replacement effect counters'")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    print(f"Query: {query}\n")

    from rules_index import embed_query
    query_emb = embed_query(query)
    results = search(query_emb, top_k=10)

    for r in results:
        print(f"  [{r['section']} — {r['title']}] score={r['score']:.3f}")
        preview = r["text"][:150].replace("\n", " ")
        print(f"    {preview}...")
        print()
