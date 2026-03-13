"""
Build vector index from rules chunks using Ollama embeddings.

Embeds each rules chunk via nomic-embed-text and saves to a JSON index
for brute-force cosine similarity search.

Usage:
    python3 rules_index.py              # build index from rules_chunks.json
    python3 rules_index.py --rebuild    # force rebuild
"""

import argparse
import json
import os
import time
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CHUNKS_FILE = os.path.join(DATA_DIR, "rules_chunks.json")
INDEX_FILE = os.path.join(DATA_DIR, "rules_index.json")

OLLAMA_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"
BATCH_SIZE = 32


def embed_texts(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    """Embed a batch of texts via Ollama /api/embed endpoint."""
    payload = json.dumps({
        "model": model,
        "input": texts,
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())

    return data["embeddings"]


def embed_query(text: str, model: str = EMBED_MODEL) -> list[float]:
    """Embed a single query string."""
    result = embed_texts([text], model)
    return result[0]


def build_index(chunks: list[dict]) -> dict:
    """Embed all chunks and build the index structure."""
    total = len(chunks)
    all_embeddings = []

    print(f"Embedding {total} chunks with {EMBED_MODEL}...")

    for i in range(0, total, BATCH_SIZE):
        batch = chunks[i:i + BATCH_SIZE]
        texts = [c["text"] for c in batch]

        try:
            embeddings = embed_texts(texts)
            all_embeddings.extend(embeddings)
            done = min(i + BATCH_SIZE, total)
            print(f"  [{done}/{total}] embedded")
        except Exception as e:
            print(f"  [ERROR at batch {i}] {e}")
            # Fill with empty embeddings for failed batch
            all_embeddings.extend([[] for _ in batch])

    # Attach embeddings to chunks
    indexed_chunks = []
    for chunk, embedding in zip(chunks, all_embeddings):
        indexed_chunks.append({
            "id": chunk["id"],
            "section": chunk["section"],
            "title": chunk["title"],
            "text": chunk["text"],
            "embedding": embedding,
        })

    dimension = len(all_embeddings[0]) if all_embeddings and all_embeddings[0] else 0

    index = {
        "model": EMBED_MODEL,
        "dimension": dimension,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "chunk_count": len(indexed_chunks),
        "chunks": indexed_chunks,
    }

    return index


def save_index(index: dict, path: str = INDEX_FILE):
    """Save index to JSON file."""
    with open(path, "w") as f:
        json.dump(index, f)
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f"Saved index to {path} ({size_mb:.1f} MB)")


def load_index(path: str = INDEX_FILE) -> dict:
    """Load index from JSON file."""
    with open(path) as f:
        return json.load(f)


def run():
    parser = argparse.ArgumentParser(description="Build rules vector index")
    parser.add_argument("--rebuild", action="store_true", help="Force rebuild even if index exists")
    args = parser.parse_args()

    if os.path.exists(INDEX_FILE) and not args.rebuild:
        print(f"Index already exists at {INDEX_FILE}")
        print(f"Use --rebuild to force rebuild")
        index = load_index()
        print(f"  Chunks: {index['chunk_count']}, Dimension: {index['dimension']}")
        print(f"  Created: {index['created']}")
        return

    if not os.path.exists(CHUNKS_FILE):
        print(f"ERROR: {CHUNKS_FILE} not found.")
        print(f"Run 'python3 rules_fetcher.py' first.")
        return

    with open(CHUNKS_FILE) as f:
        chunks = json.load(f)
    print(f"Loaded {len(chunks)} chunks from {CHUNKS_FILE}")

    index = build_index(chunks)
    save_index(index)

    # Quick sanity check
    non_empty = sum(1 for c in index["chunks"] if c["embedding"])
    print(f"\nDone: {non_empty}/{len(chunks)} chunks embedded successfully")
    print(f"Dimension: {index['dimension']}")


if __name__ == "__main__":
    run()
