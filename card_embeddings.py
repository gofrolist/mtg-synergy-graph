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
        # Replace card's own name with @ for generalization
        name = card.get("name", "")
        if name and " // " in name:
            # For multi-face cards, replace each face name
            for face in name.split(" // "):
                oracle_text = oracle_text.replace(face.strip(), "@")
        elif name:
            oracle_text = oracle_text.replace(name, "@")
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

    embeddings = np.load(EMBEDDINGS_PATH, mmap_mode='r')
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


UMAP_PATH = os.path.join(DATA_DIR, "embeddings_2d.json")
UMAP_HTML_PATH = os.path.join(DATA_DIR, "embeddings_umap.html")

# Major card type extraction order (first match wins)
MAJOR_TYPES = [
    "Creature", "Planeswalker", "Artifact", "Enchantment",
    "Instant", "Sorcery", "Land", "Battle",
]

TYPE_COLORS = {
    "Creature": "#4CAF50",
    "Planeswalker": "#9C27B0",
    "Artifact": "#78909C",
    "Enchantment": "#FF9800",
    "Instant": "#2196F3",
    "Sorcery": "#F44336",
    "Land": "#795548",
    "Battle": "#E91E63",
    "Other": "#607D8B",
}


def get_major_type(type_line: str) -> str:
    """Extract the primary card type from a type line."""
    for t in MAJOR_TYPES:
        if t in type_line:
            return t
    return "Other"


def build_umap(n_neighbors: int = 30, min_dist: float = 0.05) -> dict:
    """Reduce embeddings to 2D via UMAP and save results."""
    import umap

    embeddings, oracle_ids = load_embeddings()
    print(f"Running UMAP on {embeddings.shape[0]} cards (n_neighbors={n_neighbors}, min_dist={min_dist})...")

    reducer = umap.UMAP(
        n_neighbors=n_neighbors,
        min_dist=min_dist,
        n_components=2,
        metric="cosine",
        random_state=42,
    )
    coords_2d = reducer.fit_transform(embeddings)

    # Center the coordinates
    coords_2d -= coords_2d.mean(axis=0)

    # Load card metadata for labels
    from tag_db import get_all_cards
    all_cards = get_all_cards()
    oid_to_card = {c["oracle_id"]: c for c in all_cards}

    points = []
    for i, oid in enumerate(oracle_ids):
        card = oid_to_card.get(oid, {})
        type_line = card.get("type_line", "")
        points.append({
            "x": float(coords_2d[i, 0]),
            "y": float(coords_2d[i, 1]),
            "name": card.get("name", oid),
            "type_line": type_line,
            "major_type": get_major_type(type_line),
            "role": card.get("role", "unknown"),
            "oracle_id": oid,
        })

    with open(UMAP_PATH, "w") as f:
        json.dump(points, f)
    print(f"Saved 2D coordinates to {UMAP_PATH}")

    return {"points": points}


def generate_umap_html(deck_name: str = None):
    """Generate interactive HTML visualization of the UMAP embedding space."""
    if not os.path.exists(UMAP_PATH):
        print("No UMAP data found. Run with --umap first.")
        return

    with open(UMAP_PATH) as f:
        points = json.load(f)

    # Optionally highlight deck cards
    deck_cards = set()
    if deck_name:
        try:
            import importlib
            deck = importlib.import_module(f"decks.{deck_name}")
            deck_cards = set(deck.DECKLIST) | {deck.COMMANDER}
            print(f"Highlighting {len(deck_cards)} cards from {deck_name} deck")
        except ImportError:
            print(f"Deck '{deck_name}' not found, showing all cards")

    points_json = json.dumps(points)
    deck_json = json.dumps(list(deck_cards))
    type_colors_json = json.dumps(TYPE_COLORS)

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>MTG Card Embedding Space</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #1a1a2e; color: #eee; font-family: system-ui, sans-serif; overflow: hidden; }}
  #canvas {{ display: block; cursor: crosshair; }}
  #tooltip {{
    position: absolute; display: none; background: #16213e; border: 1px solid #0f3460;
    padding: 8px 12px; border-radius: 6px; font-size: 13px; max-width: 320px;
    pointer-events: none; z-index: 10; box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  }}
  #tooltip .name {{ font-weight: bold; font-size: 14px; margin-bottom: 4px; }}
  #tooltip .type {{ color: #aaa; font-size: 12px; }}
  #tooltip .role {{ color: #7ec8e3; font-size: 12px; }}
  #controls {{
    position: absolute; top: 12px; left: 12px; display: flex; gap: 8px; align-items: center;
  }}
  #controls button, #controls select {{
    background: #16213e; color: #eee; border: 1px solid #0f3460; padding: 6px 12px;
    border-radius: 4px; cursor: pointer; font-size: 13px;
  }}
  #controls button:hover {{ background: #0f3460; }}
  #legend {{
    position: absolute; bottom: 12px; left: 12px; background: #16213ecc;
    border: 1px solid #0f3460; border-radius: 6px; padding: 10px 14px; font-size: 12px;
  }}
  #legend div {{ display: flex; align-items: center; gap: 6px; margin: 3px 0; }}
  #legend .dot {{ width: 10px; height: 10px; border-radius: 50%; }}
  #search {{
    position: absolute; top: 12px; right: 12px;
  }}
  #search input {{
    background: #16213e; color: #eee; border: 1px solid #0f3460; padding: 6px 12px;
    border-radius: 4px; font-size: 13px; width: 220px;
  }}
  #stats {{
    position: absolute; top: 48px; right: 12px; font-size: 12px; color: #888;
  }}
</style>
</head><body>
<canvas id="canvas"></canvas>
<div id="tooltip"><div class="name"></div><div class="type"></div><div class="role"></div></div>
<div id="controls">
  <button id="resetBtn">Reset View</button>
  <select id="colorBy">
    <option value="type">Color by Type</option>
    <option value="role">Color by Role</option>
    {"<option value='deck'>Color by Deck</option>" if deck_cards else ""}
  </select>
</div>
<div id="legend"></div>
<div id="search"><input id="searchInput" type="text" placeholder="Search cards..."></div>
<div id="stats"></div>

<script>
const points = {points_json};
const deckCards = new Set({deck_json});
const typeColors = {type_colors_json};
const roleColors = {{
  "ramp": "#4CAF50", "removal": "#F44336", "draw": "#2196F3",
  "threat": "#FF9800", "combo-piece": "#9C27B0", "support": "#00BCD4",
  "protection": "#FFEB3B", "utility": "#795548", "finisher": "#E91E63",
  "enabler": "#8BC34A", "payoff": "#FF5722", "lord": "#673AB7",
  "tutor": "#3F51B5", "stax": "#607D8B", "token-generator": "#CDDC39",
  "anthem": "#FFC107", "sacrifice-outlet": "#B71C1C",
}};
const defaultRoleColor = "#607D8B";

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const tooltip = document.getElementById("tooltip");

let width, height, transform = {{ x: 0, y: 0, scale: 1 }};
let dragging = false, dragStart = {{ x: 0, y: 0 }};
let searchTerm = "", colorMode = "type";

function resize() {{
  width = canvas.width = window.innerWidth;
  height = canvas.height = window.innerHeight;
  draw();
}}

function worldToScreen(wx, wy) {{
  return {{
    x: (wx * transform.scale) + transform.x + width / 2,
    y: (wy * transform.scale) + transform.y + height / 2,
  }};
}}

function screenToWorld(sx, sy) {{
  return {{
    x: (sx - width / 2 - transform.x) / transform.scale,
    y: (sy - height / 2 - transform.y) / transform.scale,
  }};
}}

function getColor(p) {{
  if (colorMode === "deck") return deckCards.has(p.name) ? "#FFD700" : "#334";
  if (colorMode === "role") return roleColors[p.role] || defaultRoleColor;
  return typeColors[p.major_type] || typeColors["Other"];
}}

function getRadius(p) {{
  if (colorMode === "deck" && deckCards.has(p.name)) return 5;
  if (searchTerm && p.name.toLowerCase().includes(searchTerm)) return 6;
  return 2.5;
}}

function getAlpha(p) {{
  if (searchTerm && !p.name.toLowerCase().includes(searchTerm)) return 0.1;
  if (colorMode === "deck" && !deckCards.has(p.name)) return 0.15;
  return 0.85;
}}

function draw() {{
  ctx.fillStyle = "#1a1a2e";
  ctx.fillRect(0, 0, width, height);

  for (const p of points) {{
    const {{ x, y }} = worldToScreen(p.x, p.y);
    if (x < -10 || x > width + 10 || y < -10 || y > height + 10) continue;
    const r = getRadius(p) * Math.min(transform.scale / 15, 2);
    ctx.globalAlpha = getAlpha(p);
    ctx.fillStyle = getColor(p);
    ctx.beginPath();
    ctx.arc(x, y, Math.max(r, 1), 0, Math.PI * 2);
    ctx.fill();
  }}
  ctx.globalAlpha = 1;
}}

function autoFit() {{
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of points) {{
    minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
  }}
  const rangeX = maxX - minX || 1, rangeY = maxY - minY || 1;
  const padding = 0.9;
  transform.scale = Math.min(width / rangeX, height / rangeY) * padding;
  transform.x = -(minX + rangeX / 2) * transform.scale;
  transform.y = -(minY + rangeY / 2) * transform.scale;
  draw();
}}

canvas.addEventListener("wheel", (e) => {{
  e.preventDefault();
  const factor = e.deltaY > 0 ? 0.9 : 1.1;
  const wx = (e.clientX - width / 2 - transform.x) / transform.scale;
  const wy = (e.clientY - height / 2 - transform.y) / transform.scale;
  transform.scale *= factor;
  transform.x = e.clientX - width / 2 - wx * transform.scale;
  transform.y = e.clientY - height / 2 - wy * transform.scale;
  draw();
}});

canvas.addEventListener("mousedown", (e) => {{
  dragging = true;
  dragStart = {{ x: e.clientX - transform.x, y: e.clientY - transform.y }};
}});

canvas.addEventListener("mousemove", (e) => {{
  if (dragging) {{
    transform.x = e.clientX - dragStart.x;
    transform.y = e.clientY - dragStart.y;
    draw();
    return;
  }}
  // Find nearest point for tooltip
  let nearest = null, minDist = 20;
  for (const p of points) {{
    const {{ x, y }} = worldToScreen(p.x, p.y);
    const d = Math.hypot(x - e.clientX, y - e.clientY);
    if (d < minDist) {{ minDist = d; nearest = p; }}
  }}
  if (nearest) {{
    tooltip.style.display = "block";
    tooltip.style.left = (e.clientX + 14) + "px";
    tooltip.style.top = (e.clientY - 10) + "px";
    tooltip.querySelector(".name").textContent = nearest.name;
    tooltip.querySelector(".type").textContent = nearest.type_line;
    tooltip.querySelector(".role").textContent = "Role: " + nearest.role;
  }} else {{
    tooltip.style.display = "none";
  }}
}});

canvas.addEventListener("mouseup", () => {{ dragging = false; }});
canvas.addEventListener("mouseleave", () => {{ dragging = false; tooltip.style.display = "none"; }});

document.getElementById("resetBtn").addEventListener("click", autoFit);
document.getElementById("colorBy").addEventListener("change", (e) => {{
  colorMode = e.target.value;
  updateLegend();
  draw();
}});
document.getElementById("searchInput").addEventListener("input", (e) => {{
  searchTerm = e.target.value.toLowerCase();
  draw();
}});

function updateLegend() {{
  const legend = document.getElementById("legend");
  legend.innerHTML = "";
  let items = {{}};
  if (colorMode === "type") items = typeColors;
  else if (colorMode === "role") {{
    const seen = new Set(points.map(p => p.role));
    for (const r of [...seen].sort()) items[r] = roleColors[r] || defaultRoleColor;
  }} else if (colorMode === "deck") {{
    items = {{ "In deck": "#FFD700", "Other": "#334" }};
  }}
  for (const [label, color] of Object.entries(items)) {{
    const row = document.createElement("div");
    row.innerHTML = '<span class="dot" style="background:' + color + '"></span>' + label;
    legend.appendChild(row);
  }}
}}

document.getElementById("stats").textContent = points.length + " cards";
window.addEventListener("resize", resize);
resize();
updateLegend();
autoFit();
</script>
</body></html>"""

    with open(UMAP_HTML_PATH, "w") as f:
        f.write(html)
    print(f"Saved interactive visualization to {UMAP_HTML_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Card embeddings with gte-modernbert-base")
    parser.add_argument("--query", type=str, help="Find cards similar to this card")
    parser.add_argument("--top", type=int, default=20, help="Number of results")
    parser.add_argument("--stats", action="store_true", help="Show embedding stats")
    parser.add_argument("--umap", action="store_true", help="Generate UMAP 2D visualization")
    parser.add_argument("--deck", type=str, help="Highlight deck cards in UMAP (e.g. kyler)")
    parser.add_argument("--n-neighbors", type=int, default=30, help="UMAP n_neighbors parameter")
    parser.add_argument("--min-dist", type=float, default=0.05, help="UMAP min_dist parameter")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Batch size for encoding")
    args = parser.parse_args()

    if args.query:
        find_similar(args.query, args.top)
    elif args.umap:
        build_umap(n_neighbors=args.n_neighbors, min_dist=args.min_dist)
        generate_umap_html(deck_name=args.deck)
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
