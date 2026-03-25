"""N-card chain discovery and loop detection."""
from collections import defaultdict
from mtg_synergy.causal.types import Edge, Chain, ResourceDelta, LoopAnalysis
from mtg_synergy.causal.resource_flow import compute_cycle_delta
from mtg_synergy.parse.ast_types import Ability


def find_chains(commander_id: str, edges: list[Edge], max_depth: int = 5) -> list[Chain]:
    """Find linear causal chains starting from commander via DFS."""
    # Build adjacency list
    adj = defaultdict(list)
    for edge in edges:
        adj[edge.source].append(edge)

    chains = []

    def dfs(current, path, path_edges, visited):
        # Record every path of length >= 2 as a chain
        if len(path) >= 2:
            chains.append(Chain(
                cards=list(path),
                edges=list(path_edges),
                chain_type="linear",
                output=_classify_output(path_edges),
            ))
        if len(path) >= max_depth:
            return
        for edge in adj.get(current, []):
            if edge.target not in visited:
                visited.add(edge.target)
                path.append(edge.target)
                path_edges.append(edge)
                dfs(edge.target, path, path_edges, visited)
                path.pop()
                path_edges.pop()
                visited.remove(edge.target)

    dfs(commander_id, [commander_id], [], {commander_id})
    _rank_chains(chains)
    return chains


def find_loops(edges: list[Edge], abilities: dict[str, list[Ability]],
               max_loop_size: int = 5) -> list[Chain]:
    """Find cycles in the edge graph and validate resource flow."""
    adj = defaultdict(list)
    for edge in edges:
        adj[edge.source].append(edge)

    all_cards = set()
    for edge in edges:
        all_cards.add(edge.source)
        all_cards.add(edge.target)

    loops = []
    seen_loops = set()  # frozenset of card ids to deduplicate

    for start in all_cards:
        _dfs_loops(start, [start], [], {start}, adj, abilities,
                   max_loop_size, loops, seen_loops)

    return loops


def _dfs_loops(current, path, path_edges, visited, adj, abilities,
               max_size, loops, seen_loops):
    if len(path) > max_size:
        return
    for edge in adj.get(current, []):
        if edge.target in visited:
            # Back edge found — extract cycle
            if edge.target in path:
                cycle_start = path.index(edge.target)
                cycle_cards = path[cycle_start:]
                cycle_edges = path_edges[cycle_start:] + [edge]
                loop_key = frozenset(cycle_cards)
                if loop_key in seen_loops:
                    continue
                seen_loops.add(loop_key)

                # Check restrictions — once_per_turn blocks infinite loops
                has_once_per_turn = False
                cycle_abilities = []
                for card_id in cycle_cards:
                    card_abs = abilities.get(card_id, [])
                    if card_abs:
                        ab = card_abs[0]  # use first ability
                        cycle_abilities.append(ab)
                        if ab.restrictions and ab.restrictions.once_per_turn:
                            has_once_per_turn = True

                # Compute resource flow
                if cycle_abilities:
                    delta = compute_cycle_delta(cycle_abilities)
                else:
                    delta = ResourceDelta()

                if has_once_per_turn:
                    analysis = LoopAnalysis(is_infinite="potential",
                                           min_board_requirement="once-per-turn restriction")
                elif delta.is_positive:
                    analysis = LoopAnalysis(is_infinite="confirmed",
                                           resource_deltas={"mana": delta.mana,
                                                            "creatures": delta.creatures},
                                           growth_pattern="linear")
                else:
                    analysis = LoopAnalysis(is_infinite="potential",
                                           min_board_requirement="resource-negative cycle")

                loops.append(Chain(
                    cards=cycle_cards,
                    edges=cycle_edges,
                    chain_type="loop",
                    output=_classify_output(cycle_edges),
                    resource_delta=delta,
                    loop_analysis=analysis,
                ))
            continue

        visited.add(edge.target)
        path.append(edge.target)
        path_edges.append(edge)
        _dfs_loops(edge.target, path, path_edges, visited, adj, abilities,
                   max_size, loops, seen_loops)
        path.pop()
        path_edges.pop()
        visited.remove(edge.target)


def _classify_output(edges: list[Edge]) -> str:
    """Classify chain output based on edge events."""
    events = {e.detail.event for e in edges if e.detail.event}
    if "damage_dealt" in events or "life_lost" in events:
        return "damage"
    if "creature_enters" in events or "enters_the_battlefield" in events:
        return "tokens"
    if "card_drawn" in events:
        return "draw"
    return "value"


def _rank_chains(chains: list[Chain]):
    """Score chains: shorter = better, stronger edges = better."""
    for chain in chains:
        card_count = len(chain.cards)
        avg_strength = (sum(e.strength for e in chain.edges) / len(chain.edges)
                        if chain.edges else 0)
        chain.score = (
            (6 - card_count) * 3.0 +
            (4.0 if chain.chain_type == "loop" else 0) +
            avg_strength * 2.0
        )
    chains.sort(key=lambda c: c.score, reverse=True)
