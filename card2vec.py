#!/usr/bin/env python3
"""Card2Vec — learn card embeddings from deck co-occurrence.

Trains Word2Vec-style embeddings where cards appearing in the same deck
are treated as "words in a sentence". Cards that frequently appear together
get similar embeddings, capturing synergies invisible in card text.

Data sources:
  - EDHREC commander data (data/edhrec_commanders/*.json)
  - MTGJSON precon decklists (precon_decks/precon_cards tables in tags.db)

Usage:
    python3 card2vec.py                      # train from all available data
    python3 card2vec.py --dim 128            # set embedding dimension
    python3 card2vec.py --query "Sol Ring"   # find similar cards by co-occurrence
    python3 card2vec.py --stats              # show stats
    python3 card2vec.py --compare "Mindcrank" "Duskmantle Guildmage"  # similarity

No external ML dependencies — implements Skip-gram with negative sampling
from scratch using only numpy.
"""

import argparse
import json
import math
import os
import random
import sqlite3
import sys

import numpy as np

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "tags.db")
EDHREC_DIR = os.path.join(DATA_DIR, "edhrec_commanders")
EMBEDDINGS_PATH = os.path.join(DATA_DIR, "card2vec_embeddings.npy")
INDEX_PATH = os.path.join(DATA_DIR, "card2vec_index.json")


def load_decklists():
    """Load decklists from all available sources.

    Returns list of lists, where each inner list is card names in a deck.
    """
    decklists = []

    # Source 1: EDHREC commander data
    # Each commander page lists cards commonly played with that commander.
    # We treat cards with high inclusion as a "deck".
    if os.path.isdir(EDHREC_DIR):
        edhrec_count = 0
        for fname in sorted(os.listdir(EDHREC_DIR)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(EDHREC_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            container = data.get("container", {}).get("json_dict", {})
            cardlists = container.get("cardlists", [])
            card_info = container.get("card", {})
            num_decks = card_info.get("num_decks", 1) or 1

            # Collect cards with inclusion rate > 10%
            deck_cards = []
            for cl in cardlists:
                for cv in cl.get("cardviews", []):
                    name = cv.get("name", "")
                    inclusion = cv.get("inclusion", 0)
                    if name and inclusion / num_decks > 0.10:
                        deck_cards.append(name)

            # Add commander name
            cmdr_name = card_info.get("name")
            if cmdr_name and cmdr_name not in deck_cards:
                deck_cards.insert(0, cmdr_name)

            if len(deck_cards) >= 10:
                decklists.append(deck_cards)
                edhrec_count += 1

        print(f"  EDHREC: {edhrec_count} commander decklists")

    # Source 2: Precon decklists from DB
    precon_count = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        # Check if precon tables exist
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "precon_decks" in tables and "precon_cards" in tables:
            for deck_id, name in conn.execute("SELECT id, name FROM precon_decks"):
                cards = [r[0] for r in conn.execute(
                    "SELECT card_name FROM precon_cards WHERE deck_id = ?",
                    (deck_id,))]
                if len(cards) >= 10:
                    decklists.append(cards)
                    precon_count += 1
        conn.close()
    except Exception as e:
        print(f"  Precon loading error: {e}")

    if precon_count:
        print(f"  Precons: {precon_count} decklists")

    # Source 3: EDHREC synergy table if available
    synergy_count = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "edhrec_decklists" in tables:
            # Group by commander
            commanders = {}
            for slug, card in conn.execute(
                    "SELECT commander_slug, card_name FROM edhrec_decklists"):
                commanders.setdefault(slug, []).append(card)
            for slug, cards in commanders.items():
                if len(cards) >= 10:
                    decklists.append(cards)
                    synergy_count += 1
        conn.close()
    except Exception:
        pass

    if synergy_count:
        print(f"  EDHREC synergy table: {synergy_count} decklists")

    print(f"  Total: {len(decklists)} decklists")
    return decklists


def build_vocabulary(decklists, min_count=3):
    """Build vocabulary from decklists.

    Returns:
        word2idx: dict mapping card name to index
        idx2word: list mapping index to card name
        counts: dict of card name to count
    """
    counts = {}
    for deck in decklists:
        for card in deck:
            counts[card] = counts.get(card, 0) + 1

    # Filter by min_count
    vocab = [card for card, count in counts.items() if count >= min_count]
    vocab.sort()

    word2idx = {card: i for i, card in enumerate(vocab)}
    idx2word = vocab

    print(f"  Vocabulary: {len(vocab)} cards (min_count={min_count})")
    return word2idx, idx2word, counts


def generate_training_pairs(decklists, word2idx, window=None):
    """Generate (target, context) pairs from decklists.

    Since cards in a deck don't have order, we treat every pair of cards
    in the same deck as a co-occurrence. For efficiency with large decks,
    we subsample: randomly select `window` context cards per target.
    """
    pairs = []
    for deck in decklists:
        # Filter to vocabulary
        indices = [word2idx[c] for c in deck if c in word2idx]
        if len(indices) < 2:
            continue

        for i, target in enumerate(indices):
            # Sample context cards (or use all if deck is small)
            others = indices[:i] + indices[i+1:]
            if window and len(others) > window:
                context_sample = random.sample(others, window)
            else:
                context_sample = others

            for ctx in context_sample:
                pairs.append((target, ctx))

    print(f"  Training pairs: {len(pairs):,}")
    return pairs


def train_skipgram(pairs, vocab_size, dim=128, epochs=5, lr=0.025,
                   neg_samples=5, counts=None, idx2word=None):
    """Train Skip-gram with Negative Sampling (SGNS).

    Pure numpy implementation — no torch/gensim dependency.
    """
    # Initialize embeddings
    rng = np.random.default_rng(42)
    W_target = rng.normal(0, 0.1, (vocab_size, dim)).astype(np.float32)
    W_context = np.zeros((vocab_size, dim), dtype=np.float32)

    # Build negative sampling distribution (word frequency ^ 0.75)
    if counts and idx2word:
        freq = np.array([counts.get(idx2word[i], 1) for i in range(vocab_size)],
                        dtype=np.float32)
    else:
        freq = np.ones(vocab_size, dtype=np.float32)
    freq = freq ** 0.75
    neg_dist = freq / freq.sum()

    # Pre-compute negative sampling table for speed
    table_size = 100_000
    neg_table = np.zeros(table_size, dtype=np.int32)
    cumsum = np.cumsum(neg_dist)
    j = 0
    for i in range(table_size):
        while j < vocab_size - 1 and cumsum[j] < (i + 0.5) / table_size:
            j += 1
        neg_table[i] = j

    n_pairs = len(pairs)
    print(f"  Training: {n_pairs:,} pairs, {epochs} epochs, dim={dim}, neg={neg_samples}")

    for epoch in range(epochs):
        # Shuffle pairs
        random.shuffle(pairs)
        total_loss = 0.0
        lr_epoch = lr * (1.0 - epoch / epochs)  # Linear decay

        for step, (target, context) in enumerate(pairs):
            # Positive sample
            t_vec = W_target[target]
            c_vec = W_context[context]
            dot = np.dot(t_vec, c_vec)
            dot = np.clip(dot, -6, 6)
            sig = 1.0 / (1.0 + np.exp(-dot))
            grad = lr_epoch * (1.0 - sig)
            total_loss += -np.log(sig + 1e-7)

            # Update
            t_update = grad * c_vec
            c_update = grad * t_vec
            W_context[context] += c_update

            # Negative samples
            neg_indices = neg_table[rng.integers(0, table_size, size=neg_samples)]
            for neg_idx in neg_indices:
                if neg_idx == context:
                    continue
                n_vec = W_context[neg_idx]
                dot_neg = np.dot(t_vec, n_vec)
                dot_neg = np.clip(dot_neg, -6, 6)
                sig_neg = 1.0 / (1.0 + np.exp(-dot_neg))
                grad_neg = lr_epoch * (-sig_neg)
                total_loss += -np.log(1.0 - sig_neg + 1e-7)

                t_update += grad_neg * n_vec
                W_context[neg_idx] += grad_neg * t_vec

            W_target[target] += t_update

            if (step + 1) % 500_000 == 0:
                avg_loss = total_loss / (step + 1)
                pct = 100 * (step + 1) / n_pairs
                print(f"    Epoch {epoch+1} [{pct:.0f}%]: loss={avg_loss:.4f}")

        avg_loss = total_loss / max(n_pairs, 1)
        print(f"  Epoch {epoch+1}/{epochs}: avg_loss={avg_loss:.4f}, lr={lr_epoch:.4f}")

    # Final embeddings: average of target and context vectors
    embeddings = (W_target + W_context) / 2.0

    # L2 normalize
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-8)
    embeddings = embeddings / norms

    return embeddings


def save_embeddings(embeddings, idx2word):
    """Save embeddings and index."""
    np.save(EMBEDDINGS_PATH, embeddings)
    with open(INDEX_PATH, "w") as f:
        json.dump({"idx2word": idx2word,
                    "word2idx": {w: i for i, w in enumerate(idx2word)},
                    "dim": int(embeddings.shape[1])}, f)
    print(f"  Saved: {EMBEDDINGS_PATH} ({embeddings.shape})")


def load_embeddings():
    """Load saved embeddings."""
    embeddings = np.load(EMBEDDINGS_PATH)
    with open(INDEX_PATH) as f:
        index = json.load(f)
    return embeddings, index


def query_similar(card_name, top_n=20):
    """Find cards with most similar co-occurrence embeddings."""
    embeddings, index = load_embeddings()
    word2idx = index["word2idx"]

    if card_name not in word2idx:
        # Try fuzzy match
        matches = [w for w in word2idx if card_name.lower() in w.lower()]
        if matches:
            card_name = matches[0]
            print(f"  Matched: {card_name}")
        else:
            print(f"  Card not found: {card_name}")
            return

    idx = word2idx[card_name]
    vec = embeddings[idx]

    # Cosine similarity (already L2-normalized, so dot product = cosine)
    sims = embeddings @ vec
    top_indices = np.argsort(sims)[::-1][1:top_n+1]

    print(f"\n  Most similar to '{card_name}' (by deck co-occurrence):")
    for rank, i in enumerate(top_indices, 1):
        name = index["idx2word"][i]
        sim = sims[i]
        print(f"    {rank:3d}. {name:<40s} {sim:.3f}")


def compare_cards(card1, card2):
    """Compare two cards by their co-occurrence embeddings."""
    embeddings, index = load_embeddings()
    word2idx = index["word2idx"]

    for card in [card1, card2]:
        if card not in word2idx:
            matches = [w for w in word2idx if card.lower() in w.lower()]
            if matches:
                print(f"  Matched '{card}' → '{matches[0]}'")
            else:
                print(f"  Card not found: {card}")
                return

    idx1 = word2idx[card1]
    idx2 = word2idx[card2]
    sim = np.dot(embeddings[idx1], embeddings[idx2])
    print(f"\n  Similarity({card1}, {card2}) = {sim:.4f}")


def import_to_db(embeddings, idx2word):
    """Store card2vec embeddings in the SQLite DB for use by recommender."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS card2vec (
        card_name TEXT PRIMARY KEY,
        embedding BLOB
    )""")
    conn.execute("DELETE FROM card2vec")

    count = 0
    for i, name in enumerate(idx2word):
        vec = embeddings[i].tobytes()
        conn.execute("INSERT OR REPLACE INTO card2vec (card_name, embedding) VALUES (?, ?)",
                      (name, vec))
        count += 1

    conn.commit()
    conn.close()
    print(f"  Stored {count} card2vec embeddings in DB")


def show_stats():
    """Show embedding statistics."""
    if not os.path.exists(EMBEDDINGS_PATH):
        print("No embeddings found. Run: python3 card2vec.py")
        return

    embeddings, index = load_embeddings()
    print(f"  Embeddings: {embeddings.shape[0]} cards, {embeddings.shape[1]} dimensions")
    print(f"  File size: {os.path.getsize(EMBEDDINGS_PATH) / 1024 / 1024:.1f} MB")

    # Show some stats
    norms = np.linalg.norm(embeddings, axis=1)
    print(f"  Norm range: [{norms.min():.3f}, {norms.max():.3f}]")

    # Average similarity
    sample_size = min(1000, len(embeddings))
    sample_idx = np.random.choice(len(embeddings), sample_size, replace=False)
    sample = embeddings[sample_idx]
    avg_sim = (sample @ sample.T).mean()
    print(f"  Average similarity (sample): {avg_sim:.4f}")


def main():
    parser = argparse.ArgumentParser(description="Card2Vec — deck co-occurrence embeddings")
    parser.add_argument("--dim", type=int, default=128, help="Embedding dimension")
    parser.add_argument("--epochs", type=int, default=5, help="Training epochs")
    parser.add_argument("--window", type=int, default=20, help="Context window size")
    parser.add_argument("--min-count", type=int, default=3, help="Min deck appearances")
    parser.add_argument("--neg-samples", type=int, default=5, help="Negative samples")
    parser.add_argument("--query", type=str, help="Find similar cards")
    parser.add_argument("--compare", nargs=2, help="Compare two cards")
    parser.add_argument("--stats", action="store_true", help="Show stats")
    args = parser.parse_args()

    if args.query:
        query_similar(args.query)
        return

    if args.compare:
        compare_cards(args.compare[0], args.compare[1])
        return

    if args.stats:
        show_stats()
        return

    # Train
    print("Loading decklists...")
    decklists = load_decklists()
    if not decklists:
        print("No decklists found. Run scrape_edhrec_commanders.py first.")
        return

    print("Building vocabulary...")
    word2idx, idx2word, counts = build_vocabulary(decklists, min_count=args.min_count)
    if len(idx2word) < 100:
        print("Too few cards in vocabulary. Need more decklists.")
        return

    print("Generating training pairs...")
    pairs = generate_training_pairs(decklists, word2idx, window=args.window)

    print("Training Skip-gram...")
    embeddings = train_skipgram(
        pairs, len(idx2word), dim=args.dim, epochs=args.epochs,
        neg_samples=args.neg_samples, counts=counts, idx2word=idx2word)

    print("Saving embeddings...")
    save_embeddings(embeddings, idx2word)
    import_to_db(embeddings, idx2word)

    print("\nDone! Try: python3 card2vec.py --query 'Sol Ring'")


if __name__ == "__main__":
    main()
