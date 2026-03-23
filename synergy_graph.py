"""
Build a synergy graph from merged card profiles.

Creates directed edges between cards based on provides/wants relationships
and categorical clustering from Scryfall function tags.

Edge types:
  1. provides→wants: Card A provides X, Card B wants X → directed edge A→B
  2. shared-tag: Cards sharing the same Scryfall function tag → undirected edge
  3. role-complement: Cards fulfilling complementary roles → weak undirected edge

Usage:
    python3 synergy_graph.py --deck kyler              # build + print top synergies
    python3 synergy_graph.py --deck krenko --validate  # compare vs hand-curated pairs
    python3 synergy_graph.py --deck kyler --card "Hardened Scales"
    python3 synergy_graph.py --deck kyler --visualize  # interactive HTML graph
    python3 synergy_graph.py --deck kyler --export
"""

import argparse
import json
import os
from collections import defaultdict

from mtg_synergy.constants import (
    SEMANTIC_BRIDGES, TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES,
    _provides_satisfies_want,
)
from mtg_synergy.graph import (
    build_graph, build_provides_wants_edges,
    build_peer_edges, build_shared_wants_edges,
    build_embedding_edges,
)
from mtg_synergy.combos import (
    find_combos, find_combos_tiered, find_partial_combos,
    compute_strategy_relevance, find_anti_synergy,
)
from mtg_synergy.combos.display import (
    show_combos, show_combos_tiered, validate_against_curated,
)
from mtg_synergy.recommend import recommend_cards, suggest_swaps, show_swaps
from mtg_synergy.recommend.engine import _deck_card_scores, _candidate_scores
from mtg_synergy.recommend.swaps import _classify_card_slot
from mtg_synergy.recommend.affinity import _compute_commander_affinity

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_merged(path: str) -> list[dict]:
    from normalize_tags import normalize_cards

    with open(path) as f:
        cards = json.load(f)
    # Normalize provides/wants vocabulary + infer missing wants
    normalize_cards(cards)
    return cards


def show_card_synergies(graph: dict, card_name: str, top_n: int = 20):
    """Show top synergies for a specific card."""
    adj = graph["adjacency"]
    edges = adj.get(card_name, [])

    if not edges:
        print(f"No synergies found for '{card_name}'")
        # Try fuzzy match
        matches = [k for k in adj if card_name.lower() in k.lower()]
        if matches:
            print(f"Did you mean: {', '.join(matches[:5])}?")
        return

    ranked = sorted(edges, key=lambda e: e["score"], reverse=True)

    print(f"\nTop synergies for: {card_name}")
    print(f"{'─' * 60}")
    for edge in ranked[:top_n]:
        sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
        print(f"\n  {edge['target']}  (score: {edge['score']}, {sig})")
        for reason in edge["reasons"][:3]:
            print(f"    {reason}")


def show_deck_synergies(graph: dict, deck_cards: set[str], commander: str,
                        cards: list[dict] = None, top_n: int = 30):
    """Show the synergy network within the deck — which cards synergize with each other."""
    adj = graph["adjacency"]

    # Collect all edges between deck cards
    deck_edges = []
    seen = set()
    for card in deck_cards:
        for edge in adj.get(card, []):
            if edge["target"] in deck_cards:
                pair = tuple(sorted([edge["source"], edge["target"]]))
                if pair not in seen:
                    seen.add(pair)
                    deck_edges.append(edge)

    deck_edges.sort(key=lambda e: e["score"], reverse=True)

    print(f"\n{'═' * 70}")
    print(f"DECK SYNERGY MAP — {len(deck_edges)} edges between {len(deck_cards)} deck cards")
    print(f"{'═' * 70}")

    # Top edges within the deck
    print(f"\nTop {top_n} in-deck synergy pairs:")
    print(f"{'─' * 70}")
    for edge in deck_edges[:top_n]:
        sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
        print(f"  [{edge['score']:5.1f} {sig}] {edge['source']} ↔ {edge['target']}")
        for r in edge["reasons"][:2]:
            print(f"       {r}")

    # Per-card connectivity within the deck
    card_synergy = defaultdict(float)
    card_partners = defaultdict(int)
    for edge in deck_edges:
        card_synergy[edge["source"]] += edge["score"]
        card_synergy[edge["target"]] += edge["score"]
        card_partners[edge["source"]] += 1
        card_partners[edge["target"]] += 1

    ranked = sorted(card_synergy.items(), key=lambda x: x[1], reverse=True)
    print(f"\nDeck card synergy ranking (total score across in-deck edges):")
    print(f"{'─' * 70}")
    print(f"  {'Card':<35} {'Score':>7} {'Partners':>10}")
    for card, total in ranked:
        partners = card_partners[card]
        marker = " ★" if card == commander else ""
        print(f"  {card:<35} {total:7.1f} {partners:>10}{marker}")

    # Weakly connected cards (potential cuts)
    if ranked:
        median_score = ranked[len(ranked) // 2][1]
        weak = [(c, s) for c, s in ranked if s < median_score * 0.3]
        if weak:
            # Classify cards to distinguish cuttable from infrastructure
            card_list = cards or []
            slot_labels = {c: _classify_card_slot(c, card_list) for c, _ in weak}
            cuttable = [(c, s) for c, s in weak if slot_labels[c] == "spell"]
            protected = [(c, s) for c, s in weak if slot_labels[c] != "spell"]

            if cuttable:
                print(f"\nWeakly connected cards (potential cut candidates):")
                for card, total in cuttable:
                    print(f"  {card:<35} {total:7.1f} ({card_partners[card]} partners)")
            if protected:
                print(f"\nLow synergy but protected (infrastructure / lands):")
                for card, total in protected:
                    label = slot_labels[card]
                    print(f"  {card:<35} {total:7.1f} ({card_partners[card]} partners) [{label}]")



def generate_visualization(graph: dict, cards: list[dict], deck_set: set,
                           commander: str, deck_name: str, combos: list = None,
                           output_path: str = None, min_edge_score: float = 0.8,
                           tiered_combos: list = None):
    """Generate a self-contained interactive HTML visualization of the deck synergy graph."""

    card_by_name = {c["name"]: c for c in cards}
    adj = graph["adjacency"]

    # Build nodes (deck cards only)
    nodes = []
    for name in sorted(deck_set):
        card = card_by_name.get(name, {})
        edges_for_card = [e for e in adj.get(name, []) if e["target"] in deck_set]
        total_syn = sum(e["score"] for e in edges_for_card)
        nodes.append({
            "name": name,
            "role": card.get("role", "unknown"),
            "provides": card.get("provides", []),
            "wants": card.get("wants", []),
            "is_commander": name == commander,
            "edge_count": len(edges_for_card),
            "total_synergy": round(total_syn, 1),
        })

    # Build card-pair sets for tiered combo edge highlighting
    confirmed_edge_pairs: set = set()
    likely_edge_pairs: set = set()
    if tiered_combos:
        for tc in tiered_combos:
            tc_cards = tc.get("cards", [])
            pairs = set()
            for i in range(len(tc_cards)):
                for j in range(i + 1, len(tc_cards)):
                    pairs.add(tuple(sorted([tc_cards[i], tc_cards[j]])))
            if tc.get("tier") == "infinite-confirmed":
                confirmed_edge_pairs.update(pairs)
            elif tc.get("tier") == "combo-likely":
                likely_edge_pairs.update(pairs)

    # Build edges (deck-internal only, above threshold)
    edges = []
    seen = set()
    for edge in graph["edges"]:
        if edge["source"] in deck_set and edge["target"] in deck_set:
            if edge["score"] >= min_edge_score:
                key = tuple(sorted([edge["source"], edge["target"]]))
                if key not in seen:
                    seen.add(key)
                    if key in confirmed_edge_pairs:
                        combo_tier = "infinite-confirmed"
                    elif key in likely_edge_pairs:
                        combo_tier = "combo-likely"
                    else:
                        combo_tier = None
                    edges.append({
                        "source": edge["source"],
                        "target": edge["target"],
                        "score": edge["score"],
                        "signals": edge["signals"],
                        "reasons": edge["reasons"],
                        "combo_tier": combo_tier,
                    })

    # Combos (legacy triangles for the combo overlay)
    combo_data = []
    if combos:
        triangles = combos.get("triangles", []) if isinstance(combos, dict) else combos
        for combo in triangles[:20]:
            combo_data.append({
                "cards": list(combo["cards"]),
                "score": combo["score"],
                "type": combo.get("type", "synergy-triangle"),
            })

    # Tiered combos for the side panel
    tiered_combo_data = []
    if tiered_combos:
        for tc in tiered_combos:
            tiered_combo_data.append({
                "cards": tc.get("cards", []),
                "tier": tc.get("tier", "synergy"),
                "result": tc.get("result", ""),
                "reason": tc.get("reason", ""),
            })

    n_confirmed = sum(1 for tc in tiered_combos if tc.get("tier") == "infinite-confirmed") if tiered_combos else 0
    n_likely = sum(1 for tc in tiered_combos if tc.get("tier") == "combo-likely") if tiered_combos else 0

    viz_data = json.dumps({
        "nodes": nodes,
        "edges": edges,
        "combos": combo_data,
        "tiered_combos": tiered_combo_data,
        "meta": {
            "deck": deck_name,
            "commander": commander,
            "total_cards": len(nodes),
            "total_edges": len(edges),
            "confirmed_combos": n_confirmed,
            "likely_combos": n_likely,
        },
    })

    html = _VIZ_HTML_TEMPLATE.replace("__GRAPH_DATA__", viz_data)

    if not output_path:
        output_path = os.path.join(DATA_DIR, f"{deck_name}_synergy_viz.html")
    with open(output_path, "w") as f:
        f.write(html)
    print(f"\nVisualization written to {output_path}")
    print(f"  {len(nodes)} nodes, {len(edges)} edges, {len(combo_data)} combos")
    if n_confirmed:
        print(f"  Spellbook confirmed combos: {n_confirmed} (highlighted gold in visualization)")
    if n_likely:
        print(f"  Likely combos: {n_likely} (highlighted orange in visualization)")


_VIZ_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MTG Synergy Graph</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #e0e0e0; overflow: hidden; }
#graph-container { width: 100vw; height: 100vh; }
svg { width: 100%; height: 100%; }

/* Controls */
#controls { position: fixed; top: 12px; left: 12px; z-index: 10; display: flex; flex-direction: column; gap: 8px; }
#search-box { width: 260px; padding: 8px 12px; border-radius: 6px; border: 1px solid #444; background: #16213e; color: #e0e0e0; font-size: 14px; }
#search-box::placeholder { color: #888; }
#search-results { background: #16213e; border: 1px solid #444; border-radius: 6px; max-height: 200px; overflow-y: auto; display: none; }
#search-results div { padding: 6px 12px; cursor: pointer; font-size: 13px; }
#search-results div:hover { background: #0f3460; }

.controls-row { display: flex; gap: 6px; flex-wrap: wrap; }
.ctrl-btn { padding: 4px 10px; border-radius: 4px; border: 1px solid #444; background: #16213e; color: #ccc; font-size: 11px; cursor: pointer; }
.ctrl-btn:hover { background: #0f3460; }
.ctrl-btn.active { background: #0f3460; border-color: #e94560; color: #fff; }

#score-slider-container { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #aaa; }
#score-slider { width: 140px; accent-color: #e94560; }

/* Side panel */
#side-panel { position: fixed; top: 0; right: -360px; width: 360px; height: 100vh; background: #16213e; border-left: 2px solid #0f3460; padding: 20px; overflow-y: auto; transition: right 0.3s; z-index: 20; }
#side-panel.open { right: 0; }
#panel-close { position: absolute; top: 10px; right: 14px; cursor: pointer; font-size: 20px; color: #888; }
#panel-close:hover { color: #e94560; }
#panel-card-name { font-size: 18px; font-weight: 700; margin-bottom: 4px; color: #fff; }
#panel-role { font-size: 13px; color: #aaa; margin-bottom: 12px; }
.panel-section { margin-bottom: 14px; }
.panel-section h4 { font-size: 12px; text-transform: uppercase; color: #e94560; margin-bottom: 4px; letter-spacing: 0.5px; }
.panel-section .tag { display: inline-block; padding: 2px 8px; margin: 2px; border-radius: 3px; background: #1a1a3e; font-size: 12px; border: 1px solid #333; }
.panel-section .tag.provides { border-color: #4CAF50; color: #81C784; }
.panel-section .tag.wants { border-color: #FF9800; color: #FFB74D; }
.panel-section .tag.synergy { border-color: #2196F3; color: #64B5F6; }
#panel-connections { font-size: 13px; }
#panel-connections .conn { padding: 4px 0; border-bottom: 1px solid #222; display: flex; justify-content: space-between; }
#panel-connections .conn-name { cursor: pointer; }
#panel-connections .conn-name:hover { color: #e94560; }
#panel-connections .conn-score { color: #888; font-size: 12px; }
#panel-notes { font-size: 13px; color: #bbb; line-height: 1.4; font-style: italic; }

/* Legend */
#legend { position: fixed; bottom: 12px; left: 12px; background: rgba(22,33,62,0.9); border: 1px solid #333; border-radius: 6px; padding: 10px 14px; z-index: 10; font-size: 11px; }
#legend h5 { margin-bottom: 6px; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; }
.legend-item { display: flex; align-items: center; gap: 6px; margin: 3px 0; }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; }

/* Tooltip */
#tooltip { position: fixed; background: rgba(22,33,62,0.95); border: 1px solid #444; border-radius: 6px; padding: 8px 12px; font-size: 12px; pointer-events: none; display: none; z-index: 30; max-width: 350px; }
#tooltip .tt-header { font-weight: 600; margin-bottom: 4px; }
#tooltip .tt-reason { color: #aaa; margin: 2px 0; }

/* Stats bar */
#stats-bar { position: fixed; bottom: 12px; right: 12px; font-size: 11px; color: #666; z-index: 10; text-align: right; }
</style>
</head>
<body>
<div id="graph-container"><svg></svg></div>

<div id="controls">
  <input id="search-box" type="text" placeholder="Search cards...">
  <div id="search-results"></div>
  <div class="controls-row" id="role-filters"></div>
  <div id="score-slider-container">
    <span>Min score:</span>
    <input id="score-slider" type="range" min="0" max="15" step="0.5" value="0.8">
    <span id="score-value">0.8</span>
  </div>
  <div class="controls-row">
    <button class="ctrl-btn" id="btn-combos">Show Combos</button>
    <button class="ctrl-btn" id="btn-labels">Labels</button>
    <button class="ctrl-btn" id="btn-reset">Reset View</button>
  </div>
</div>

<div id="side-panel">
  <span id="panel-close">&times;</span>
  <div id="panel-card-name"></div>
  <div id="panel-role"></div>
  <div class="panel-section"><h4>Provides</h4><div id="panel-provides"></div></div>
  <div class="panel-section"><h4>Wants</h4><div id="panel-wants"></div></div>
  <div class="panel-section"><h4>Synergy Tags</h4><div id="panel-synergy"></div></div>
  <div class="panel-section"><h4>Notes</h4><div id="panel-notes"></div></div>
  <div class="panel-section"><h4>Connections</h4><div id="panel-connections"></div></div>
</div>

<div id="legend">
  <h5>Roles</h5>
  <div id="legend-items"></div>
</div>

<div id="tooltip">
  <div class="tt-header"></div>
  <div class="tt-body"></div>
</div>

<div id="stats-bar"></div>

<script>
const DATA = __GRAPH_DATA__;

const ROLE_COLORS = {
  enabler:    "#4CAF50",
  threat:     "#f44336",
  ramp:       "#8BC34A",
  removal:    "#FF9800",
  protection: "#2196F3",
  draw:       "#9C27B0",
  utility:    "#00BCD4",
  tutor:      "#795548",
  land:       "#607D8B",
  unknown:    "#9E9E9E",
};

const width = window.innerWidth;
const height = window.innerHeight;
const svg = d3.select("svg");
const g = svg.append("g");

// Zoom
const zoom = d3.zoom().scaleExtent([0.2, 5]).on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

// State
let showLabels = false;
let showCombos = false;
let scoreThreshold = 0.8;
let selectedNode = null;
let activeRoles = new Set(Object.keys(ROLE_COLORS));

// Prep data
const nodeMap = {};
DATA.nodes.forEach(n => { nodeMap[n.name] = n; });
let visibleEdges = DATA.edges.filter(e => e.score >= scoreThreshold);

// Edge lookup
function getEdgesForNode(name) {
  return DATA.edges.filter(e => (e.source.name || e.source) === name || (e.target.name || e.target) === name);
}

function nodeRadius(d) {
  let r = 6 + Math.sqrt(d.total_synergy) * 1.2;
  if (d.is_commander) r *= 1.4;
  return Math.min(r, 30);
}

// Build role filters
const roles = [...new Set(DATA.nodes.map(n => n.role))].sort();
const roleFilters = d3.select("#role-filters");
roles.forEach(role => {
  const btn = roleFilters.append("button")
    .attr("class", "ctrl-btn active")
    .style("border-left", `3px solid ${ROLE_COLORS[role] || ROLE_COLORS.unknown}`)
    .text(role)
    .on("click", function() {
      if (activeRoles.has(role)) { activeRoles.delete(role); d3.select(this).classed("active", false); }
      else { activeRoles.add(role); d3.select(this).classed("active", true); }
      updateVisibility();
    });
});

// Legend
const legendItems = d3.select("#legend-items");
roles.forEach(role => {
  const item = legendItems.append("div").attr("class", "legend-item");
  item.append("div").attr("class", "legend-dot").style("background", ROLE_COLORS[role] || ROLE_COLORS.unknown);
  item.append("span").text(role);
});
// Edge color legend
const edgeLegend = legendItems.append("div").attr("class", "legend-item").style("margin-top", "6px");
edgeLegend.append("div").style("width", "24px").style("height", "3px").style("background", "#FFD700").style("border-radius", "2px");
edgeLegend.append("span").text("Spellbook combo");
const edgeLegend2 = legendItems.append("div").attr("class", "legend-item");
edgeLegend2.append("div").style("width", "24px").style("height", "3px").style("background", "#FF8C00").style("border-radius", "2px");
edgeLegend2.append("span").text("Likely combo");

// Stats
const comboStats = DATA.meta.confirmed_combos > 0
  ? ` | ${DATA.meta.confirmed_combos} confirmed combos` + (DATA.meta.likely_combos > 0 ? ` | ${DATA.meta.likely_combos} likely` : "")
  : "";
d3.select("#stats-bar").html(
  `${DATA.meta.deck} | ${DATA.meta.total_cards} cards | ${DATA.meta.total_edges} edges | Commander: ${DATA.meta.commander}${comboStats}`
);

// Force simulation
const simulation = d3.forceSimulation(DATA.nodes)
  .force("link", d3.forceLink(visibleEdges).id(d => d.name).distance(d => Math.max(60, 180 - d.score * 8)).strength(d => Math.min(0.5, d.score / 20)))
  .force("charge", d3.forceManyBody().strength(-180))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .force("collision", d3.forceCollide().radius(d => nodeRadius(d) + 3))
  .alphaDecay(0.02);

function edgeBaseColor(d) {
  if (d.combo_tier === "infinite-confirmed") return "#FFD700";
  if (d.combo_tier === "combo-likely") return "#FF8C00";
  return "#555";
}
function edgeBaseOpacity(d) {
  if (d.combo_tier === "infinite-confirmed") return 0.75;
  if (d.combo_tier === "combo-likely") return 0.55;
  return Math.max(0.08, Math.min(0.6, d.score / 15));
}
function edgeBaseWidth(d) {
  if (d.combo_tier === "infinite-confirmed") return Math.max(2, Math.min(5, d.score / 3));
  if (d.combo_tier === "combo-likely") return Math.max(1.5, Math.min(4, d.score / 3));
  return Math.max(0.5, Math.min(4, d.score / 3));
}

// Render edges
const edgeGroup = g.append("g").attr("class", "edges");
let edgeElements = edgeGroup.selectAll("line").data(visibleEdges).join("line")
  .attr("stroke", d => edgeBaseColor(d))
  .attr("stroke-width", d => edgeBaseWidth(d))
  .attr("stroke-opacity", d => edgeBaseOpacity(d));

// Combo overlays
const comboGroup = g.append("g").attr("class", "combos").style("display", "none");

// Render nodes
const nodeGroup = g.append("g").attr("class", "nodes");
const nodeElements = nodeGroup.selectAll("g").data(DATA.nodes).join("g")
  .call(d3.drag()
    .on("start", (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on("end", (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; })
  );

nodeElements.append("circle")
  .attr("r", d => nodeRadius(d))
  .attr("fill", d => ROLE_COLORS[d.role] || ROLE_COLORS.unknown)
  .attr("stroke", d => d.is_commander ? "#FFD700" : "#333")
  .attr("stroke-width", d => d.is_commander ? 3 : 1)
  .style("cursor", "pointer");

// Labels
const labelElements = nodeElements.append("text")
  .text(d => d.name)
  .attr("dy", d => nodeRadius(d) + 12)
  .attr("text-anchor", "middle")
  .attr("fill", "#ccc")
  .attr("font-size", "10px")
  .style("pointer-events", "none")
  .style("display", "none");

// Commander label always visible
labelElements.filter(d => d.is_commander).style("display", "block").attr("font-size", "12px").attr("font-weight", "700").attr("fill", "#FFD700");

// Tooltip
const tooltip = d3.select("#tooltip");

edgeGroup.selectAll("line")
  .on("mouseover", (e, d) => {
    const src = d.source.name || d.source;
    const tgt = d.target.name || d.target;
    tooltip.select(".tt-header").text(`${src} ↔ ${tgt} (${d.score})`);
    tooltip.select(".tt-body").html(d.reasons.map(r => `<div class="tt-reason">${r}</div>`).join(""));
    tooltip.style("display", "block").style("left", (e.clientX + 15) + "px").style("top", (e.clientY - 10) + "px");
  })
  .on("mousemove", e => {
    tooltip.style("left", (e.clientX + 15) + "px").style("top", (e.clientY - 10) + "px");
  })
  .on("mouseout", () => tooltip.style("display", "none"));

// Node click
nodeElements.on("click", (e, d) => {
  e.stopPropagation();
  selectNode(d);
});

svg.on("click", () => clearSelection());

function selectNode(d) {
  selectedNode = d;
  const connected = new Set();
  const connEdges = [];
  DATA.edges.forEach(e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    if (src === d.name) { connected.add(tgt); connEdges.push({name: tgt, score: e.score, reasons: e.reasons}); }
    if (tgt === d.name) { connected.add(src); connEdges.push({name: src, score: e.score, reasons: e.reasons}); }
  });
  connected.add(d.name);

  // Dim non-connected
  nodeElements.select("circle").attr("opacity", n => connected.has(n.name) ? 1 : 0.1);
  labelElements.style("display", n => connected.has(n.name) ? "block" : "none");
  edgeElements.attr("stroke-opacity", e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    return (src === d.name || tgt === d.name) ? 0.9 : 0.02;
  }).attr("stroke", e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    if (src === d.name || tgt === d.name) return "#e94560";
    return edgeBaseColor(e);
  }).attr("stroke-width", e => {
    const src = e.source.name || e.source;
    const tgt = e.target.name || e.target;
    return (src === d.name || tgt === d.name) ? Math.max(1.5, Math.min(5, e.score / 3)) : edgeBaseWidth(e);
  });

  // Side panel
  connEdges.sort((a, b) => b.score - a.score);
  d3.select("#panel-card-name").text(d.name);
  d3.select("#panel-role").text(`Role: ${d.role} | Edges: ${d.edge_count} | Total synergy: ${d.total_synergy}`);
  d3.select("#panel-provides").html(d.provides.map(t => `<span class="tag provides">${t}</span>`).join(""));
  d3.select("#panel-wants").html(d.wants.map(t => `<span class="tag wants">${t}</span>`).join(""));
  d3.select("#panel-connections").html(
    connEdges.slice(0, 20).map(c => `<div class="conn"><span class="conn-name" data-name="${c.name}">${c.name}</span><span class="conn-score">${c.score}</span></div>`).join("")
  );
  // Click connection names
  d3.selectAll(".conn-name").on("click", function() {
    const name = this.dataset.name;
    const node = DATA.nodes.find(n => n.name === name);
    if (node) selectNode(node);
  });
  d3.select("#side-panel").classed("open", true);
}

function clearSelection() {
  selectedNode = null;
  nodeElements.select("circle").attr("opacity", 1);
  if (!showLabels) labelElements.filter(d => !d.is_commander).style("display", "none");
  edgeElements
    .attr("stroke-opacity", d => edgeBaseOpacity(d))
    .attr("stroke", d => edgeBaseColor(d))
    .attr("stroke-width", d => edgeBaseWidth(d));
  d3.select("#side-panel").classed("open", false);
}

// Score slider
d3.select("#score-slider").on("input", function() {
  scoreThreshold = +this.value;
  d3.select("#score-value").text(scoreThreshold);
  updateEdges();
});

function updateEdges() {
  visibleEdges = DATA.edges.filter(e => e.score >= scoreThreshold &&
    activeRoles.has((nodeMap[e.source.name || e.source] || {}).role) &&
    activeRoles.has((nodeMap[e.target.name || e.target] || {}).role));

  edgeElements = edgeGroup.selectAll("line").data(visibleEdges, d => (d.source.name || d.source) + "-" + (d.target.name || d.target));
  edgeElements.exit().remove();
  const newEdges = edgeElements.enter().append("line")
    .attr("stroke", d => edgeBaseColor(d))
    .attr("stroke-width", d => edgeBaseWidth(d))
    .attr("stroke-opacity", d => edgeBaseOpacity(d))
    .on("mouseover", (ev, d) => {
      const src = d.source.name || d.source;
      const tgt = d.target.name || d.target;
      tooltip.select(".tt-header").text(`${src} ↔ ${tgt} (${d.score})`);
      tooltip.select(".tt-body").html(d.reasons.map(r => `<div class="tt-reason">${r}</div>`).join(""));
      tooltip.style("display", "block").style("left", (ev.clientX + 15) + "px").style("top", (ev.clientY - 10) + "px");
    })
    .on("mousemove", ev => tooltip.style("left", (ev.clientX + 15) + "px").style("top", (ev.clientY - 10) + "px"))
    .on("mouseout", () => tooltip.style("display", "none"));
  edgeElements = newEdges.merge(edgeElements);

  simulation.force("link", d3.forceLink(visibleEdges).id(d => d.name).distance(d => Math.max(60, 180 - d.score * 8)).strength(d => Math.min(0.5, d.score / 20)));
  simulation.alpha(0.3).restart();
}

function updateVisibility() {
  nodeElements.style("display", d => activeRoles.has(d.role) ? "block" : "none");
  updateEdges();
}

// Labels toggle
d3.select("#btn-labels").on("click", function() {
  showLabels = !showLabels;
  d3.select(this).classed("active", showLabels);
  if (showLabels) labelElements.style("display", "block");
  else { labelElements.filter(d => !d.is_commander && !(selectedNode && d.name === selectedNode.name)).style("display", "none"); }
});

// Combos toggle
d3.select("#btn-combos").on("click", function() {
  showCombos = !showCombos;
  d3.select(this).classed("active", showCombos);
  comboGroup.style("display", showCombos ? "block" : "none");
  if (showCombos) renderCombos();
});

function renderCombos() {
  comboGroup.selectAll("*").remove();
  // Legacy synergy triangles
  if (DATA.combos.length) {
    const comboColors = { "infinite-combo": "#e94560", "sac-combo": "#FF5722", "counter-combo": "#4CAF50", "token-combo": "#FFEB3B", "etb-combo": "#2196F3", "tribal-combo": "#9C27B0", "damage-combo": "#f44336" };
    DATA.combos.forEach(combo => {
      const positions = combo.cards.map(name => DATA.nodes.find(n => n.name === name)).filter(Boolean);
      if (positions.length >= 3) {
        const color = comboColors[combo.type] || "#e94560";
        comboGroup.append("polygon")
          .datum(positions)
          .attr("fill", color)
          .attr("fill-opacity", 0.12)
          .attr("stroke", color)
          .attr("stroke-width", 2)
          .attr("stroke-opacity", 0.5)
          .attr("stroke-dasharray", "4,2");
      }
    });
  }
  // Tiered combos: confirmed (gold) and likely (orange) overlays
  if (DATA.tiered_combos && DATA.tiered_combos.length) {
    DATA.tiered_combos.forEach(tc => {
      const positions = tc.cards.map(name => DATA.nodes.find(n => n.name === name)).filter(Boolean);
      if (positions.length < 2) return;
      const isConfirmed = tc.tier === "infinite-confirmed";
      const isLikely = tc.tier === "combo-likely";
      if (!isConfirmed && !isLikely) return;
      const color = isConfirmed ? "#FFD700" : "#FF8C00";
      const opacity = isConfirmed ? 0.18 : 0.12;
      if (positions.length === 2) {
        // Draw a thick highlighted line between the two cards
        comboGroup.append("line")
          .attr("stroke", color)
          .attr("stroke-width", isConfirmed ? 4 : 3)
          .attr("stroke-opacity", isConfirmed ? 0.8 : 0.6)
          .attr("stroke-dasharray", isConfirmed ? "none" : "6,3")
          .datum(positions);
      } else {
        comboGroup.append("polygon")
          .datum(positions)
          .attr("fill", color)
          .attr("fill-opacity", opacity)
          .attr("stroke", color)
          .attr("stroke-width", isConfirmed ? 3 : 2)
          .attr("stroke-opacity", isConfirmed ? 0.8 : 0.6)
          .attr("stroke-dasharray", isConfirmed ? "none" : "6,3");
      }
    });
  }
}

// Reset
d3.select("#btn-reset").on("click", () => {
  clearSelection();
  svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity);
  activeRoles = new Set(Object.keys(ROLE_COLORS));
  roleFilters.selectAll(".ctrl-btn").classed("active", true);
  d3.select("#score-slider").property("value", 0.8);
  scoreThreshold = 0.8;
  d3.select("#score-value").text("0.8");
  updateEdges();
  updateVisibility();
});

// Search
const searchBox = d3.select("#search-box");
const searchResults = d3.select("#search-results");

searchBox.on("input", function() {
  const q = this.value.toLowerCase();
  if (q.length < 2) { searchResults.style("display", "none"); return; }
  const matches = DATA.nodes.filter(n => n.name.toLowerCase().includes(q)).slice(0, 10);
  searchResults.html("").style("display", matches.length ? "block" : "none");
  matches.forEach(m => {
    searchResults.append("div").text(m.name).on("click", () => {
      selectNode(m);
      searchBox.property("value", "");
      searchResults.style("display", "none");
      // Center on node
      const t = d3.zoomIdentity.translate(width/2 - m.x, height/2 - m.y);
      svg.transition().duration(500).call(zoom.transform, t);
    });
  });
});

// Node hover: show name
nodeElements
  .on("mouseover", (e, d) => {
    if (!showLabels) labelElements.filter(n => n.name === d.name).style("display", "block");
  })
  .on("mouseout", (e, d) => {
    if (!showLabels && !d.is_commander && !(selectedNode && selectedNode.name === d.name))
      labelElements.filter(n => n.name === d.name).style("display", "none");
  });

// Tick
simulation.on("tick", () => {
  edgeElements
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x).attr("y2", d => d.target.y);

  nodeElements.attr("transform", d => `translate(${d.x},${d.y})`);

  // Update combo polygons and lines
  if (showCombos) {
    comboGroup.selectAll("polygon").attr("points", d =>
      d.map(n => `${n.x},${n.y}`).join(" ")
    );
    comboGroup.selectAll("line")
      .attr("x1", d => d[0].x).attr("y1", d => d[0].y)
      .attr("x2", d => d[1].x).attr("y2", d => d[1].y);
  }
});
</script>
</body>
</html>"""


def _detect_deck_types(cards: list[dict], deck_cards: set[str],
                       threshold: float = 0.3) -> set[str]:
    """Auto-detect dominant creature types in the deck.

    If >30% of creatures share a type, it's a tribal deck for that type.
    Returns set of dominant types (e.g. {'Human'}) or empty set.
    """
    from collections import Counter
    type_counts = Counter()
    creature_count = 0

    for c in cards:
        if c["name"] not in deck_cards:
            continue
        type_line = c.get("type_line", "")
        if "Creature" not in type_line:
            continue
        creature_count += 1
        if " — " in type_line:
            subtypes = type_line.split(" — ")[1].split()
            for st in subtypes:
                type_counts[st.strip(",")] += 1

    if creature_count == 0:
        return set()

    dominant = set()
    for t, count in type_counts.items():
        if count / creature_count >= threshold:
            dominant.add(t)

    if dominant:
        print(f"  Detected tribal types: {', '.join(sorted(dominant))} "
              f"(>{threshold:.0%} of {creature_count} creatures)")

    return dominant


def _filter_candidates(candidates: list[dict], color_identity: set[str],
                       db_path: str = None) -> list[dict]:
    """Filter candidates by color identity, commander legality, and paper availability.

    Uses Scryfall metadata from the DB (backfilled via tag_db.py backfill).
    """
    import sqlite3
    if db_path is None:
        from tag_db import DB_PATH
        db_path = DB_PATH

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Batch-load metadata for all candidates
    oids = [c["oracle_id"] for c in candidates]
    filtered = []

    chunk_size = 500
    legal_oids = set()
    for i in range(0, len(oids), chunk_size):
        chunk = oids[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT oracle_id, color_identity, legal_commander FROM cards "
            f"WHERE oracle_id IN ({placeholders})", chunk
        ).fetchall()
        for row in rows:
            # Check commander legality
            if not row["legal_commander"]:
                continue
            # Check color identity subset
            try:
                card_colors = set(json.loads(row["color_identity"]))
            except (json.JSONDecodeError, TypeError):
                card_colors = set()
            if card_colors <= color_identity:
                legal_oids.add(row["oracle_id"])

    conn.close()

    filtered = [c for c in candidates if c["oracle_id"] in legal_oids]
    return filtered


def _find_embedding_candidates(deck_cards: list[dict], deck_oids: set[str],
                               db_path: str, top_per_card: int = 3,
                               min_similarity: float = 0.70) -> list[dict]:
    """Find recommendation candidates via embedding similarity.

    For each deck card, finds top-N most similar cards not already in the deck.
    Returns deduplicated list of candidate cards loaded from DB.
    """
    try:
        from card_embeddings import load_embeddings
        import numpy as np
    except ImportError:
        return []

    import os
    emb_path = os.path.join(DATA_DIR, "embeddings.npy")
    if not os.path.exists(emb_path):
        return []

    embeddings, oracle_ids = load_embeddings()
    oid_to_idx = {oid: i for i, oid in enumerate(oracle_ids)}

    # Get indices for deck cards
    deck_indices = []
    for card in deck_cards:
        idx = oid_to_idx.get(card["oracle_id"])
        if idx is not None:
            deck_indices.append(idx)

    if not deck_indices:
        return []

    # Compute average deck embedding for centroid-based search
    deck_matrix = embeddings[np.array(deck_indices)]
    deck_centroid = deck_matrix.mean(axis=0)
    deck_centroid = deck_centroid / np.linalg.norm(deck_centroid)

    # Find cards similar to the deck centroid
    all_sims = embeddings @ deck_centroid

    # Also find per-card similar cards (catches specific synergies)
    candidate_oids = set()
    for deck_idx in deck_indices:
        card_sims = embeddings[deck_idx] @ embeddings.T
        top_idx = np.argpartition(-card_sims, top_per_card + 1)[:top_per_card + 1]
        for idx in top_idx:
            oid = oracle_ids[idx]
            if oid not in deck_oids and card_sims[idx] >= min_similarity:
                candidate_oids.add(oid)

    # Also add top centroid-similar cards
    centroid_top = np.argpartition(-all_sims, 100)[:100]
    for idx in centroid_top:
        oid = oracle_ids[idx]
        if oid not in deck_oids and all_sims[idx] >= min_similarity:
            candidate_oids.add(oid)

    if not candidate_oids:
        return []

    from tag_db import get_cards_by_oids
    return get_cards_by_oids(list(candidate_oids), db_path)


def build_from_commander(commander_name: str, top_n: int = 30):
    """Build a deck recommendation from scratch based on commander card alone.

    1. Load commander from DB, read its provides/wants
    2. Extract creature types from commander's type_line
    3. Find all commander-legal cards in the commander's color identity
    4. Score each card by how well it connects to the commander's strategy
    5. Group and display by strategy
    """
    import sqlite3
    from tag_db import DB_PATH, get_cards_by_names

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Find commander
    row = conn.execute("SELECT * FROM cards WHERE name = ?", (commander_name,)).fetchone()
    if not row:
        # Try fuzzy match
        row = conn.execute("SELECT * FROM cards WHERE name LIKE ?",
                          (f"%{commander_name}%",)).fetchone()
    if not row:
        print(f"Commander not found: {commander_name}")
        return

    cmd_oid = row["oracle_id"]
    cmd_name = row["name"]
    cmd_type = row["type_line"]
    cmd_text = row["oracle_text"]
    try:
        cmd_colors = set(json.loads(row["color_identity"]))
    except (json.JSONDecodeError, TypeError):
        cmd_colors = set()

    # Get commander's tags
    cmd_provides = [r[0] for r in conn.execute(
        "SELECT tag FROM provides WHERE oracle_id=?", (cmd_oid,))]
    cmd_wants = [r[0] for r in conn.execute(
        "SELECT tag FROM wants WHERE oracle_id=?", (cmd_oid,))]

    # Extract creature types from commander
    cmd_subtypes = set()
    if " — " in cmd_type:
        cmd_subtypes = {s.strip(",") for s in cmd_type.split(" — ")[1].split()}

    # Build expanded wants: what the commander wants + semantic bridges from provides
    # e.g. commander provides token-generation → also find cards wanting token-events, creature-board
    expanded_wants = set(cmd_wants)
    for p_tag in cmd_provides:
        for (bridge_p, bridge_w), weight in SEMANTIC_BRIDGES.items():
            if bridge_p == p_tag and weight >= 0.5:
                expanded_wants.add(bridge_w)

    # Build expanded provides: what the commander provides + semantic bridges from wants
    expanded_provides = set(cmd_provides)
    for w_tag in cmd_wants:
        for (bridge_p, bridge_w), weight in SEMANTIC_BRIDGES.items():
            if bridge_w == w_tag and weight >= 0.5:
                expanded_provides.add(bridge_p)

    print(f"\n{'═' * 70}")
    print(f"COMMANDER: {cmd_name}")
    print(f"  {cmd_type} | CMC {row['cmc']}")
    print(f"  {cmd_text}")
    print(f"  Colors: {','.join(sorted(cmd_colors)) or 'C'}")
    print(f"  Provides: {cmd_provides}")
    print(f"  Wants: {cmd_wants}")
    if expanded_wants - set(cmd_wants):
        print(f"  Expanded wants (via bridges): {sorted(expanded_wants - set(cmd_wants))}")
    if expanded_provides - set(cmd_provides):
        print(f"  Expanded provides (via bridges): {sorted(expanded_provides - set(cmd_provides))}")
    if cmd_subtypes:
        print(f"  Creature types: {', '.join(sorted(cmd_subtypes))}")
    print(f"{'═' * 70}")

    # Load ALL legal cards in commander's colors from DB
    all_rows = conn.execute(
        "SELECT oracle_id, name, type_line, cmc, mana_cost, color_identity, oracle_text "
        "FROM cards WHERE legal_commander = 1 AND oracle_id != ?",
        (cmd_oid,)
    ).fetchall()

    # Filter by color identity
    legal_cards = {}
    for r in all_rows:
        try:
            card_colors = set(json.loads(r["color_identity"]))
        except (json.JSONDecodeError, TypeError):
            card_colors = set()
        if card_colors <= cmd_colors:
            legal_cards[r["oracle_id"]] = {
                "name": r["name"], "type_line": r["type_line"],
                "cmc": r["cmc"], "mana_cost": r["mana_cost"] or "",
                "oracle_text": r["oracle_text"] or "",
            }

    # Load all provides/wants for legal cards
    legal_oids = list(legal_cards.keys())
    card_provides = defaultdict(set)
    card_wants = defaultdict(set)

    chunk_size = 500
    for i in range(0, len(legal_oids), chunk_size):
        chunk = legal_oids[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT oracle_id, tag FROM provides WHERE oracle_id IN ({placeholders})", chunk
        ):
            card_provides[r[0]].add(r[1])
        for r in conn.execute(
            f"SELECT oracle_id, tag FROM wants WHERE oracle_id IN ({placeholders})", chunk
        ):
            card_wants[r[0]].add(r[1])

    conn.close()

    # Score each card
    scores = {}
    for oid, meta in legal_cards.items():
        name = meta["name"]
        c_provides = card_provides.get(oid, set())
        c_wants = card_wants.get(oid, set())

        enabler_tags = []  # this card provides what commander wants
        payoff_tags = []   # this card wants what commander provides
        score = 0.0

        # Exact + semantic: card provides what commander wants
        for p_tag in c_provides:
            if p_tag in expanded_wants:
                weight = 1.0 if p_tag in cmd_wants else 0.7  # bridge match = lower weight
                score += weight
                enabler_tags.append(p_tag)

        # Exact + semantic: card wants what commander provides
        for w_tag in c_wants:
            if w_tag in expanded_provides:
                weight = 1.0 if w_tag in cmd_provides else 0.7
                score += weight
                payoff_tags.append(w_tag)

        # Tribal boost
        tribal = False
        if cmd_subtypes and any(t in meta["type_line"] for t in cmd_subtypes):
            score *= 1.5
            tribal = True

        if score > 0:
            scores[name] = {
                "score": round(score, 1),
                "type_line": meta["type_line"],
                "cmc": meta["cmc"],
                "mana_cost": meta["mana_cost"],
                "enabler_tags": enabler_tags,
                "payoff_tags": payoff_tags,
                "tribal": tribal,
                "is_enabler": len(enabler_tags) > 0,
                "is_payoff": len(payoff_tags) > 0,
            }

    ranked = sorted(scores.items(), key=lambda x: -x[1]["score"])

    # Split into categories
    both = [(n, s) for n, s in ranked if s["is_enabler"] and s["is_payoff"]]
    enablers_only = [(n, s) for n, s in ranked if s["is_enabler"] and not s["is_payoff"]]
    payoffs_only = [(n, s) for n, s in ranked if s["is_payoff"] and not s["is_enabler"]]

    print(f"\nFound {len(scores)} synergy cards ({len(both)} both, "
          f"{len(enablers_only)} enablers, {len(payoffs_only)} payoffs)")

    # Display BEST FIT first
    if both:
        print(f"\nBEST FIT — enable AND benefit from {cmd_name} ({len(both)} found)")
        print(f"{'─' * 70}")
        for name, info in both[:top_n]:
            tribal = " [tribal]" if info["tribal"] else ""
            e_tags = ", ".join(info["enabler_tags"])
            p_tags = ", ".join(info["payoff_tags"])
            print(f"  {name}{tribal}  (score {info['score']})")
            print(f"    {info['type_line']} | CMC {info['cmc']}")
            print(f"    enables: {e_tags} | benefits: {p_tags}")

    if enablers_only:
        print(f"\nENABLERS — provide what {cmd_name} wants ({len(enablers_only)} found)")
        print(f"{'─' * 70}")
        for name, info in enablers_only[:top_n]:
            tribal = " [tribal]" if info["tribal"] else ""
            tags = ", ".join(info["enabler_tags"])
            print(f"  {name}{tribal}  (score {info['score']})")
            print(f"    {info['type_line']} | CMC {info['cmc']}")
            print(f"    enables: {tags}")

    if payoffs_only:
        print(f"\nPAYOFFS — benefit from {cmd_name} ({len(payoffs_only)} found)")
        print(f"{'─' * 70}")
        for name, info in payoffs_only[:top_n]:
            tribal = " [tribal]" if info["tribal"] else ""
            tags = ", ".join(info["payoff_tags"])
            print(f"  {name}{tribal}  (score {info['score']})")
            print(f"    {info['type_line']} | CMC {info['cmc']}")
            print(f"    benefits from: {tags}")


def show_deck_analysis(deck_cards, deck_oids, active_strategies, commander_name, db_path=None, graph=None, deck_set=None):
    """Enhanced deck analysis with strategy coverage."""
    import sqlite3
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "data", "tags.db")
    conn = sqlite3.connect(db_path)

    # Count cards per strategy
    strat_counts = {}
    for oid in deck_oids:
        for row in conn.execute(
            "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3", (oid,)
        ):
            strat_counts[row[0]] = strat_counts.get(row[0], 0) + 1

    # Count non-land cards
    non_land = sum(1 for c in deck_cards if "Land" not in (c.get("type_line") or ""))

    # Count strategy-aligned cards
    aligned = 0
    if active_strategies:
        placeholders = ','.join('?' * len(active_strategies))
        for oid in deck_oids:
            rows = conn.execute(
                f"SELECT 1 FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3 AND strategy IN ({placeholders})",
                (oid, *active_strategies)
            ).fetchall()
            if rows:
                aligned += 1

    combos = find_combos_tiered(deck_oids, db_path)
    anti = find_anti_synergy(deck_oids, active_strategies, db_path, graph=graph, deck_cards_set=deck_set)
    conn.close()

    print(f"\n{'='*60}")
    print(f"DECK ANALYSIS: {commander_name}")
    print(f"{'='*60}")

    print(f"Detected strategies:")
    for strat in sorted(active_strategies):
        cnt = strat_counts.get(strat, 0)
        if cnt > 0:
            print(f"  {strat}: {cnt} cards")

    coverage = aligned * 100 // max(non_land, 1)
    print(f"Strategy coverage: {coverage}% of {non_land} non-land cards align with >=1 strategy")

    confirmed = sum(1 for c in combos if c["tier"] == "infinite-confirmed")
    likely = sum(1 for c in combos if c["tier"] == "combo-likely")
    synergy = sum(1 for c in combos if c["tier"] == "synergy")
    print(f"Confirmed combos: {confirmed} (Spellbook)")
    print(f"Likely combos: {likely} (trigger chain)")
    print(f"Synergy pairs: {synergy}")

    if anti:
        print(f"Anti-synergy cards: {len(anti)} (swap candidates)")
        for a in anti[:5]:
            print(f"  {a['name']} ({a['role'] or 'unknown'}) — {a['partners']} partners, score {a['synergy_score']}")


def run():
    from decks import list_decks

    parser = argparse.ArgumentParser(description="Build MTG synergy graph")
    parser.add_argument("--deck", choices=list_decks(), help="Deck config to use")
    parser.add_argument("--commander", type=str, help="Build recommendations from commander alone")
    parser.add_argument("--build", action="store_true",
                        help="Build deck from commander (use with --commander)")
    parser.add_argument("--input", type=str, help="Override: load cards from JSON file instead of DB")
    parser.add_argument("--card", type=str, help="Show synergies for specific card")
    parser.add_argument("--deck-view", action="store_true",
                        help="Show synergy network within the deck")
    parser.add_argument("--recommend", action="store_true",
                        help="Recommend cards based on synergy with the deck")
    parser.add_argument("--combos", action="store_true",
                        help="Detect 3- and 4-card combos in the deck")
    parser.add_argument("--swaps", action="store_true",
                        help="Suggest card swaps to improve deck synergy")
    parser.add_argument("--validate", action="store_true",
                        help="Validate against hand-curated synergy pairs")
    parser.add_argument("--visualize", action="store_true",
                        help="Generate interactive HTML visualization")
    parser.add_argument("--export", action="store_true", help="Export graph as JSON")
    parser.add_argument("--top", type=int, default=30, help="Top N edges to show")
    parser.add_argument("--strategies", default="auto",
                        help="Comma-separated strategies to focus (default: auto-detect)")
    parser.add_argument("--exclude-strategies", default=None,
                        help="Comma-separated strategies to exclude")
    args = parser.parse_args()

    # Commander build mode — no deck needed
    if args.commander and args.build:
        build_from_commander(args.commander, args.top)
        return

    if not args.deck:
        parser.error("--deck is required (or use --commander with --build)")

    if args.input:
        # Manual override: load from JSON file
        cards = load_merged(args.input)
        print(f"Loaded {len(cards)} cards from {args.input}")
    else:
        # Default: load deck cards from SQLite DB
        from tag_db import get_cards_by_names, find_synergy_candidates, DB_PATH
        from decks import load_deck
        deck = load_deck(args.deck)
        deck_names = deck.DECKLIST + [deck.COMMANDER]

        cards = get_cards_by_names(deck_names, DB_PATH)
        print(f"Loaded {len(cards)} deck cards from DB")

        if args.recommend or args.swaps:
            # Find synergy candidates from DB (targeted + commander bridge expansion)
            commander_card = next((c for c in cards if c["name"] == deck.COMMANDER), None)
            candidates = find_synergy_candidates(cards, DB_PATH, commander=commander_card)
            print(f"Found {len(candidates)} tag-based candidates from DB")
            deck_oids = {c["oracle_id"] for c in cards}

            # Hybrid: also find candidates via embedding similarity
            emb_candidates = _find_embedding_candidates(cards, deck_oids, DB_PATH)
            if emb_candidates:
                print(f"Found {len(emb_candidates)} embedding-based candidates")

            # Filter candidates by color identity + commander legality
            color_id = deck.COLOR_IDENTITY
            candidates = _filter_candidates(candidates, color_id, DB_PATH)
            if emb_candidates:
                emb_candidates = _filter_candidates(emb_candidates, color_id, DB_PATH)
            print(f"After filter (color={','.join(sorted(color_id))}, legal, paper): "
                  f"{len(candidates)} tag + {len(emb_candidates)} embedding candidates")

            # Merge: union of tag-based and embedding-based candidates
            all_candidate_oids = set()
            for c in candidates:
                if c["oracle_id"] not in deck_oids:
                    all_candidate_oids.add(c["oracle_id"])
                    cards.append(c)
            for c in emb_candidates:
                if c["oracle_id"] not in deck_oids and c["oracle_id"] not in all_candidate_oids:
                    cards.append(c)

            print(f"Building graph for {len(cards)} cards (deck + candidates)")

    # --- Strategy detection ---
    active_strategies = set()
    db_path = None
    if not args.input:
        from tag_db import DB_PATH as _db_path
        db_path = _db_path
        from strategy_detector import detect_strategies
        commander_card = next((c for c in cards if c["name"] == deck.COMMANDER), None)
        commander_oid = commander_card["oracle_id"] if commander_card else None
        if args.strategies == "auto" and commander_oid:
            detected = detect_strategies(commander_oid, db_path)
            active_strategies = {s["name"] for s in detected if s["confidence"] >= 0.3}
            # Also detect strategies from deck composition:
            # 1. Tribal strategies from creature type distribution
            deck_names_set = set(deck.DECKLIST) | {deck.COMMANDER}
            deck_cards_for_types = [c for c in cards if c["name"] in deck_names_set]
            deck_types = _detect_deck_types(deck_cards_for_types, deck_names_set)
            if deck_types:
                from strategy_detector import CREATURE_TYPE_STRATEGIES
                import sqlite3 as _sqlite3
                _conn = _sqlite3.connect(db_path)
                for dtype in deck_types:
                    strat = CREATURE_TYPE_STRATEGIES.get(dtype.lower())
                    if strat and strat not in active_strategies:
                        has_cards = _conn.execute(
                            "SELECT 1 FROM card_strategies WHERE strategy = ? LIMIT 1",
                            (strat,)
                        ).fetchone()
                        if has_cards:
                            active_strategies.add(strat)
                _conn.close()

            # 2. Strategies shared by 20%+ of non-land deck cards
            import sqlite3 as _sqlite3
            _conn = _sqlite3.connect(db_path)
            deck_oid_set = {c["oracle_id"] for c in cards if c["name"] in deck_names_set}
            non_land_count = sum(1 for c in cards
                                 if c["name"] in deck_names_set and "Land" not in c.get("type_line", ""))
            if non_land_count > 0:
                from collections import Counter as _Counter
                strat_counts = _Counter()
                for oid in deck_oid_set:
                    for row in _conn.execute(
                        "SELECT strategy FROM card_strategies WHERE oracle_id = ? AND confidence >= 0.3",
                        (oid,)
                    ):
                        strat_counts[row[0]] += 1
                for strat, cnt in strat_counts.items():
                    if cnt / non_land_count >= 0.2 and strat not in active_strategies:
                        active_strategies.add(strat)
            _conn.close()
        elif args.strategies != "auto":
            active_strategies = set(args.strategies.split(","))
        if args.exclude_strategies:
            active_strategies -= set(args.exclude_strategies.split(","))
        if active_strategies:
            print(f"Active strategies: {', '.join(sorted(active_strategies))}")

    # Collect deck oracle_ids for fan-out cap preservation
    _build_deck_oids = None
    if not args.input:
        deck_names_for_oids = set(deck.DECKLIST) | {deck.COMMANDER}
        _build_deck_oids = {c["oracle_id"] for c in cards if c["name"] in deck_names_for_oids}

    graph = build_graph(cards, deck_oids=_build_deck_oids)
    stats = graph["stats"]
    print(f"\nGraph stats:")
    print(f"  raw signal edges:      {stats['total_raw_edges']}")
    print(f"    provides→wants:      {stats['provides_wants_edges']}")
    print(f"    peer-enabler:        {stats['peer_enabler_edges']}")
    print(f"    shared-wants:        {stats['shared_wants_edges']}")
    print(f"    embedding:           {stats.get('embedding_edges', 0)}")
    print(f"  composite edges:       {stats['pruned_edges']} (unique card pairs)")
    print(f"  cards with edges:      {stats['cards_with_edges']}/{stats['cards_total']}")

    # Ensure deck config is loaded (already set in DB path, need it for --input path)
    if args.input:
        from decks import load_deck
        deck = load_deck(args.deck)

    if args.card:
        show_card_synergies(graph, args.card)
    elif args.visualize:
        deck_set = set(deck.DECKLIST) | {deck.COMMANDER}
        combos = find_combos(graph, cards, deck_set, deck.COMMANDER, top_n=20)
        # Enrich with Spellbook / inferred tiered combo data if DB is available
        tiered = None
        if db_path:
            deck_oids = {c["oracle_id"] for c in cards if c["name"] in deck_set}
            tiered = find_combos_tiered(deck_oids, db_path)
            confirmed = [c for c in tiered if c["tier"] == "infinite-confirmed"]
            if confirmed:
                print(f"\n  Spellbook confirmed combos: {len(confirmed)} (highlighted in visualization)")
        generate_visualization(graph, cards, deck_set, deck.COMMANDER, args.deck, combos,
                               tiered_combos=tiered)
    elif args.deck_view or args.recommend or args.combos or args.swaps:
        deck_set = set(deck.DECKLIST) | {deck.COMMANDER}
        deck_oids = {c["oracle_id"] for c in cards if c["name"] in deck_set}
        if args.deck_view:
            show_deck_synergies(graph, deck_set, deck.COMMANDER, cards, args.top)
            if db_path and active_strategies:
                deck_cards_in_set = [c for c in cards if c["name"] in deck_set]
                show_deck_analysis(deck_cards_in_set, deck_oids, active_strategies, deck.COMMANDER, db_path, graph=graph, deck_set=deck_set)
        if args.combos:
            if db_path:
                # Use enhanced 3-tier combo detection
                show_combos_tiered(deck_oids, deck.COMMANDER, db_path, color_identity=deck.COLOR_IDENTITY)
            else:
                # Fallback to legacy combo detection
                combos = find_combos(graph, cards, deck_set, deck.COMMANDER, args.top)
                show_combos(combos, deck.COMMANDER, args.top)
        if args.swaps:
            swap_deck_types = _detect_deck_types(cards, deck_set)
            swaps = suggest_swaps(graph, deck_set, deck.COMMANDER, cards, args.top,
                                  active_strategies=active_strategies, db_path=db_path,
                                  deck_types=swap_deck_types)
            show_swaps(swaps, args.top)
        if args.recommend:
            # Auto-detect dominant creature types for tribal boost
            deck_types = _detect_deck_types(cards, deck_set)
            recommend_cards(graph, deck_set, cards, deck_types, args.top,
                            active_strategies=active_strategies, db_path=db_path,
                            color_identity=deck.COLOR_IDENTITY, commander=deck.COMMANDER)
    elif args.validate:
        validate_against_curated(graph, deck.SYNERGY_PAIRS)
    elif args.export:
        graph_output = os.path.join(DATA_DIR, f"{args.deck}_synergy_graph.json")
        export = {
            "edges": graph["edges"],
            "stats": graph["stats"],
        }
        with open(graph_output, "w") as f:
            json.dump(export, f, indent=2)
        print(f"\nExported graph to {graph_output}")
    else:
        # Show top edges
        print(f"\nTop {args.top} synergy edges:")
        print(f"{'─' * 70}")
        for edge in graph["edges"][:args.top]:
            sig = f"{edge['signals']}sig" if edge["signals"] > 1 else "1sig"
            print(f"  [{edge['score']:5.1f} {sig}] {edge['source']} ↔ {edge['target']}")
            for r in edge["reasons"][:2]:
                print(f"       {r}")


if __name__ == "__main__":
    run()
