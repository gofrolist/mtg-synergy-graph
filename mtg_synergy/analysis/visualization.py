"""Interactive HTML visualization of the synergy graph."""
import json
import os

from mtg_synergy.config import DATA_DIR


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
