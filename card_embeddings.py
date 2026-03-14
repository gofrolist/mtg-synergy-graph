"""
Card embeddings — serialize MTG cards as prettified JSON and embed with gte-modernbert-base.

Produces 768-dim L2-normalized vectors for all cards in the tag database.
Embeddings are saved as numpy arrays alongside an oracle_id index for lookup.

Usage:
    python3 card_embeddings.py                    # embed all cards in tags.db
    python3 card_embeddings.py --query "Sol Ring"  # find similar cards
    python3 card_embeddings.py --stats             # show embedding stats

Requires: pip install torch sentence-transformers numpy
"""

import argparse
import json
import os
import sys

import numpy as np

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
EMBEDDINGS_PATH = os.path.join(DATA_DIR, "embeddings.npy")
INDEX_PATH = os.path.join(DATA_DIR, "embeddings_index.json")

MODEL_NAME = "Alibaba-NLP/gte-modernbert-base"
EMBEDDING_DIM = 768
BATCH_SIZE = 64
MAX_LENGTH = 8192


def serialize_card(card: dict) -> str:
    """Serialize a card as prettified JSON for embedding.

    Includes all mechanically relevant fields: name, mana cost, type line,
    oracle text, power/toughness, keywords, color identity.
    Follows the mtg-embeddings approach of structured JSON representation.
    """
    # Build a clean dict with only relevant fields
    data = {"name": card.get("name", "")}

    if card.get("mana_cost"):
        data["manaCost"] = card["mana_cost"]
    if card.get("cmc") is not None:
        data["cmc"] = card["cmc"]

    type_line = card.get("type_line", "")
    if type_line:
        data["type"] = type_line

    oracle_text = card.get("oracle_text", "")
    if oracle_text:
        # Replace card's own name with ~ for generalization
        name = card.get("name", "")
        if name and " // " in name:
            # For multi-face cards, replace each face name
            for face in name.split(" // "):
                oracle_text = oracle_text.replace(face.strip(), "~")
        elif name:
            oracle_text = oracle_text.replace(name, "~")
        data["text"] = oracle_text

    if card.get("power") is not None:
        data["power"] = str(card["power"])
    if card.get("toughness") is not None:
        data["toughness"] = str(card["toughness"])
    if card.get("loyalty") is not None:
        data["loyalty"] = str(card["loyalty"])

    keywords = card.get("keywords", [])
    if keywords:
        data["keywords"] = keywords

    color_identity = card.get("color_identity", [])
    if color_identity:
        data["colorIdentity"] = color_identity

    return json.dumps(data, indent=2)


def load_cards_for_embedding() -> list[dict]:
    """Load all cards from tag DB + Scryfall bulk data for full card info.

    Merges tag_db cards (oracle_id, name, type_line, oracle_text) with
    Scryfall bulk data (mana_cost, power, toughness, keywords, color_identity).
    """
    from tag_db import get_all_cards

    db_cards = get_all_cards()
    print(f"Loaded {len(db_cards)} cards from tag DB")

    # Try to enrich with Scryfall bulk data for mana_cost, p/t, keywords
    scryfall_path = os.path.join(DATA_DIR, "oracle_cards.json")
    scryfall_data = {}
    if os.path.exists(scryfall_path):
        with open(scryfall_path) as f:
            for card in json.load(f):
                oid = card.get("oracle_id")
                if oid:
                    scryfall_data[oid] = card
        print(f"Enriching with Scryfall data ({len(scryfall_data)} cards)")

    enriched = []
    for card in db_cards:
        oid = card["oracle_id"]
        sf = scryfall_data.get(oid, {})

        # Merge Scryfall fields into tag DB card
        card["mana_cost"] = sf.get("mana_cost", "")
        card["cmc"] = sf.get("cmc", 0)
        card["keywords"] = sf.get("keywords", [])
        card["color_identity"] = sf.get("color_identity", [])

        # Power/toughness from card faces or top-level
        if "card_faces" in sf:
            face = sf["card_faces"][0]
            card["power"] = face.get("power")
            card["toughness"] = face.get("toughness")
            card["loyalty"] = face.get("loyalty")
        else:
            card["power"] = sf.get("power")
            card["toughness"] = sf.get("toughness")
            card["loyalty"] = sf.get("loyalty")

        enriched.append(card)

    return enriched


def embed_cards(cards: list[dict], batch_size: int = BATCH_SIZE) -> np.ndarray:
    """Embed cards using gte-modernbert-base. Returns (N, 768) float32 array."""
    from sentence_transformers import SentenceTransformer

    print(f"Loading model {MODEL_NAME}...")
    model = SentenceTransformer(MODEL_NAME)

    # Serialize all cards
    texts = [serialize_card(card) for card in cards]
    print(f"Embedding {len(texts)} cards (batch_size={batch_size})...")

    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=True,
        normalize_embeddings=True,  # L2 normalize for cosine similarity via dot product
    )

    return embeddings.astype(np.float32)


def save_embeddings(embeddings: np.ndarray, oracle_ids: list[str]):
    """Save embeddings and oracle_id index."""
    np.save(EMBEDDINGS_PATH, embeddings)

    index = {"oracle_ids": oracle_ids}
    with open(INDEX_PATH, "w") as f:
        json.dump(index, f)

    size_mb = os.path.getsize(EMBEDDINGS_PATH) / (1024 * 1024)
    print(f"Saved {len(oracle_ids)} embeddings to {EMBEDDINGS_PATH} ({size_mb:.1f} MB)")
    print(f"Saved index to {INDEX_PATH}")


def load_embeddings() -> tuple[np.ndarray, list[str]]:
    """Load embeddings and oracle_id index. Returns (embeddings, oracle_ids)."""
    if not os.path.exists(EMBEDDINGS_PATH) or not os.path.exists(INDEX_PATH):
        print(f"No embeddings found. Run: python3 card_embeddings.py", file=sys.stderr)
        sys.exit(1)

    embeddings = np.load(EMBEDDINGS_PATH)
    with open(INDEX_PATH) as f:
        index = json.load(f)

    return embeddings, index["oracle_ids"]


def find_similar(query_name: str, top_n: int = 20, color_filter: list[str] = None):
    """Find cards most similar to a query card by embedding similarity."""
    embeddings, oracle_ids = load_embeddings()

    # Build name lookup
    from tag_db import get_all_cards
    all_cards = get_all_cards()
    name_to_idx = {}
    idx_to_card = {}
    for i, oid in enumerate(oracle_ids):
        for card in all_cards:
            if card["oracle_id"] == oid:
                name_to_idx[card["name"].lower()] = i
                idx_to_card[i] = card
                break

    query_lower = query_name.lower()
    if query_lower not in name_to_idx:
        # Fuzzy match
        matches = [n for n in name_to_idx if query_lower in n]
        if matches:
            print(f"Did you mean: {', '.join(matches[:5])}?")
        else:
            print(f"Card '{query_name}' not found in embeddings")
        return

    query_idx = name_to_idx[query_lower]
    query_vec = embeddings[query_idx]

    # Dot product (embeddings are L2-normalized, so this is cosine similarity)
    similarities = embeddings @ query_vec

    # Get top results (excluding self)
    top_indices = np.argpartition(-similarities, top_n + 1)[:top_n + 1]
    top_indices = top_indices[top_indices != query_idx]
    top_indices = sorted(top_indices, key=lambda i: -similarities[i])[:top_n]

    query_card = idx_to_card.get(query_idx, {})
    print(f"\nCards most similar to: {query_card.get('name', query_name)}")
    print(f"{'─' * 60}")
    for idx in top_indices:
        card = idx_to_card.get(idx, {})
        sim = similarities[idx]
        print(f"  {sim:.3f}  {card.get('name', oracle_ids[idx])}")
        if card.get("type_line"):
            print(f"         {card['type_line']}")


def get_embedding_similarity(oid_a: str, oid_b: str,
                             embeddings: np.ndarray = None,
                             oracle_ids: list[str] = None) -> float:
    """Get cosine similarity between two cards by oracle_id."""
    if embeddings is None or oracle_ids is None:
        embeddings, oracle_ids = load_embeddings()

    oid_to_idx = {oid: i for i, oid in enumerate(oracle_ids)}
    idx_a = oid_to_idx.get(oid_a)
    idx_b = oid_to_idx.get(oid_b)

    if idx_a is None or idx_b is None:
        return 0.0

    return float(embeddings[idx_a] @ embeddings[idx_b])


def main():
    parser = argparse.ArgumentParser(description="Card embeddings with gte-modernbert-base")
    parser.add_argument("--query", type=str, help="Find cards similar to this card")
    parser.add_argument("--top", type=int, default=20, help="Number of results")
    parser.add_argument("--stats", action="store_true", help="Show embedding stats")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Batch size for encoding")
    args = parser.parse_args()

    if args.query:
        find_similar(args.query, args.top)
    elif args.stats:
        embeddings, oracle_ids = load_embeddings()
        size_mb = os.path.getsize(EMBEDDINGS_PATH) / (1024 * 1024)
        print(f"Embeddings: {embeddings.shape[0]} cards, {embeddings.shape[1]} dimensions")
        print(f"File size: {size_mb:.1f} MB")
        print(f"Dtype: {embeddings.dtype}")

        # Sanity check: norms should be ~1.0 (L2-normalized)
        norms = np.linalg.norm(embeddings, axis=1)
        print(f"L2 norms: mean={norms.mean():.4f}, min={norms.min():.4f}, max={norms.max():.4f}")
    else:
        # Build embeddings
        cards = load_cards_for_embedding()
        embeddings = embed_cards(cards, batch_size=args.batch_size)
        oracle_ids = [c["oracle_id"] for c in cards]
        save_embeddings(embeddings, oracle_ids)


if __name__ == "__main__":
    main()
