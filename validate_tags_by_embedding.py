"""
Validate card tags by comparing against nearest embedding neighbors.

For each card, finds the K most similar cards by embedding cosine similarity
and checks if the card's tags (role, provides, wants) are consistent with
its neighbors. Flags outliers where a card's tags disagree with what similar
cards have.

Usage:
    python3 validate_tags_by_embedding.py                              # validate all
    python3 validate_tags_by_embedding.py --card "Counterspell"        # check one card
    python3 validate_tags_by_embedding.py --role-only                  # only check roles
    python3 validate_tags_by_embedding.py --top 50                     # show top 50 outliers
    python3 validate_tags_by_embedding.py --tags-file data/top10000_tags_finetuned.json
"""

import argparse
import json
import os

import numpy as np

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_all(tags_path: str):
    """Load embeddings, tags, and build lookup indices."""
    from card_embeddings import load_embeddings

    embeddings, oracle_ids = load_embeddings()
    oid_to_idx = {oid: i for i, oid in enumerate(oracle_ids)}

    with open(tags_path) as f:
        tags_list = json.load(f)
    tags_by_oid = {c["oracle_id"]: c for c in tags_list if "oracle_id" in c}

    return embeddings, oracle_ids, oid_to_idx, tags_by_oid


def get_neighbors(embeddings, oracle_ids, oid_to_idx, query_oid, k=10):
    """Get K nearest neighbors by embedding similarity."""
    idx = oid_to_idx.get(query_oid)
    if idx is None:
        return []

    sims = embeddings[idx] @ embeddings.T
    sims[idx] = -1.0  # exclude self

    top_idx = np.argpartition(-sims, k)[:k]
    top_idx = sorted(top_idx, key=lambda i: -sims[i])

    return [(oracle_ids[i], float(sims[i])) for i in top_idx]


def check_role_consistency(card, neighbors, tags_by_oid):
    """Check if card's role is consistent with its neighbors' roles."""
    card_role = card.get("role", "")
    neighbor_roles = []

    for oid, sim in neighbors:
        n = tags_by_oid.get(oid)
        if n and sim >= 0.70:
            neighbor_roles.append((n.get("role", ""), sim, n.get("name", "")))

    if not neighbor_roles:
        return None

    # Count role votes weighted by similarity
    from collections import Counter
    role_votes = Counter()
    for role, sim, _ in neighbor_roles:
        role_votes[role] += sim

    top_role, top_weight = role_votes.most_common(1)[0]
    total_weight = sum(role_votes.values())
    top_pct = top_weight / total_weight if total_weight > 0 else 0

    if card_role != top_role and top_pct >= 0.5:
        # Majority of similar cards have a different role
        same_role = [(r, s, n) for r, s, n in neighbor_roles if r == card_role]
        diff_role = [(r, s, n) for r, s, n in neighbor_roles if r == top_role]
        return {
            "type": "role",
            "current": card_role,
            "suggested": top_role,
            "confidence": round(top_pct, 2),
            "evidence": [f"{n} ({s:.0%})" for _, s, n in diff_role[:5]],
            "supporting": [f"{n} ({s:.0%})" for _, s, n in same_role[:3]],
        }
    return None


def check_provides_consistency(card, neighbors, tags_by_oid):
    """Check if card is missing provides tags that all its neighbors have."""
    card_provides = set(card.get("provides", []))
    issues = []

    # Count how many neighbors have each provides tag
    from collections import Counter
    tag_counts = Counter()
    tag_examples = {}
    n_counted = 0

    for oid, sim in neighbors:
        n = tags_by_oid.get(oid)
        if n and sim >= 0.75:
            n_counted += 1
            for tag in n.get("provides", []):
                tag_counts[tag] += 1
                if tag not in tag_examples:
                    tag_examples[tag] = []
                tag_examples[tag].append((n.get("name", ""), sim))

    if n_counted < 3:
        return issues

    # Tags that most neighbors have but this card doesn't
    for tag, count in tag_counts.most_common():
        pct = count / n_counted
        if pct >= 0.6 and tag not in card_provides:
            examples = tag_examples[tag][:3]
            issues.append({
                "type": "missing_provides",
                "tag": tag,
                "neighbor_pct": round(pct, 2),
                "evidence": [f"{n} ({s:.0%})" for n, s in examples],
            })

    # Tags this card has but no neighbors do (possible hallucination)
    for tag in card_provides:
        if tag_counts.get(tag, 0) == 0 and n_counted >= 5:
            issues.append({
                "type": "suspect_provides",
                "tag": tag,
                "neighbor_pct": 0,
                "evidence": [f"0/{n_counted} neighbors have this tag"],
            })

    return issues


def check_wants_consistency(card, neighbors, tags_by_oid):
    """Check if card is missing wants tags that most neighbors share."""
    card_wants = set(card.get("wants", []))
    issues = []

    from collections import Counter
    tag_counts = Counter()
    tag_examples = {}
    n_counted = 0

    for oid, sim in neighbors:
        n = tags_by_oid.get(oid)
        if n and sim >= 0.75:
            n_counted += 1
            for tag in n.get("wants", []):
                tag_counts[tag] += 1
                if tag not in tag_examples:
                    tag_examples[tag] = []
                tag_examples[tag].append((n.get("name", ""), sim))

    if n_counted < 3:
        return issues

    for tag, count in tag_counts.most_common():
        pct = count / n_counted
        if pct >= 0.6 and tag not in card_wants:
            examples = tag_examples[tag][:3]
            issues.append({
                "type": "missing_wants",
                "tag": tag,
                "neighbor_pct": round(pct, 2),
                "evidence": [f"{n} ({s:.0%})" for n, s in examples],
            })

    return issues


def validate_card(card, embeddings, oracle_ids, oid_to_idx, tags_by_oid, k=10):
    """Run all consistency checks for a single card."""
    oid = card.get("oracle_id")
    if not oid:
        return []

    neighbors = get_neighbors(embeddings, oracle_ids, oid_to_idx, oid, k=k)
    if not neighbors:
        return []

    issues = []

    role_issue = check_role_consistency(card, neighbors, tags_by_oid)
    if role_issue:
        issues.append(role_issue)

    issues.extend(check_provides_consistency(card, neighbors, tags_by_oid))
    issues.extend(check_wants_consistency(card, neighbors, tags_by_oid))

    return issues


def main():
    parser = argparse.ArgumentParser(description="Validate tags by embedding neighbors")
    parser.add_argument("--tags-file", default=os.path.join(DATA_DIR, "top10000_tags.json"))
    parser.add_argument("--card", help="Check a single card")
    parser.add_argument("--role-only", action="store_true", help="Only check role consistency")
    parser.add_argument("--top", type=int, default=30, help="Show top N outliers")
    parser.add_argument("--neighbors", type=int, default=10, help="Number of neighbors to check")
    parser.add_argument("--min-confidence", type=float, default=0.5,
                        help="Min confidence for role suggestions")
    args = parser.parse_args()

    print(f"Loading embeddings and tags from {args.tags_file}...")
    embeddings, oracle_ids, oid_to_idx, tags_by_oid = load_all(args.tags_file)
    print(f"  {len(oracle_ids)} embeddings, {len(tags_by_oid)} tagged cards")

    if args.card:
        # Single card mode
        card = None
        for c in tags_by_oid.values():
            if c.get("name", "").lower() == args.card.lower():
                card = c
                break
        if not card:
            print(f"Card '{args.card}' not found")
            return

        neighbors = get_neighbors(embeddings, oracle_ids, oid_to_idx, card["oracle_id"], k=args.neighbors)
        print(f"\n{card['name']}")
        print(f"  role: {card.get('role')}")
        print(f"  provides: {card.get('provides', [])}")
        print(f"  wants: {card.get('wants', [])}")
        print(f"\n  Nearest neighbors:")
        for oid, sim in neighbors:
            n = tags_by_oid.get(oid, {})
            print(f"    {sim:.0%}  {n.get('name', oid):<35} role={n.get('role', '?'):<12} "
                  f"provides={n.get('provides', [])[:3]}")

        issues = validate_card(card, embeddings, oracle_ids, oid_to_idx, tags_by_oid, k=args.neighbors)
        if issues:
            print(f"\n  Issues found:")
            for issue in issues:
                _print_issue(card["name"], issue)
        else:
            print(f"\n  No issues found — tags are consistent with neighbors")
        return

    # Batch mode: check all cards
    print(f"Checking all cards (k={args.neighbors})...")
    all_issues = []

    for oid, card in tags_by_oid.items():
        if oid not in oid_to_idx:
            continue

        if args.role_only:
            neighbors = get_neighbors(embeddings, oracle_ids, oid_to_idx, oid, k=args.neighbors)
            issue = check_role_consistency(card, neighbors, tags_by_oid)
            if issue and issue["confidence"] >= args.min_confidence:
                all_issues.append((card["name"], card.get("edhrec_rank", 99999), [issue]))
        else:
            issues = validate_card(card, embeddings, oracle_ids, oid_to_idx, tags_by_oid, k=args.neighbors)
            if issues:
                all_issues.append((card["name"], card.get("edhrec_rank", 99999), issues))

    # Sort by EDHREC rank (most popular cards first)
    all_issues.sort(key=lambda x: x[1])

    print(f"\nFound {len(all_issues)} cards with potential tag issues")
    print(f"Showing top {args.top} by EDHREC rank:\n")

    for name, rank, issues in all_issues[:args.top]:
        card = next(c for c in tags_by_oid.values() if c.get("name") == name)
        print(f"{'─' * 70}")
        print(f"  {name} (rank #{rank})")
        print(f"  current: role={card.get('role')} provides={card.get('provides', [])[:4]}")
        for issue in issues[:3]:
            _print_issue(name, issue)


def _print_issue(name, issue):
    if issue["type"] == "role":
        print(f"    ⚠ ROLE: {issue['current']} → {issue['suggested']} "
              f"(confidence: {issue['confidence']:.0%})")
        print(f"      neighbors: {', '.join(issue['evidence'][:3])}")
        if issue["supporting"]:
            print(f"      supporting current: {', '.join(issue['supporting'][:2])}")
    elif issue["type"] == "missing_provides":
        print(f"    ⚠ MISSING PROVIDES: '{issue['tag']}' "
              f"({issue['neighbor_pct']:.0%} of neighbors have it)")
        print(f"      e.g. {', '.join(issue['evidence'][:3])}")
    elif issue["type"] == "suspect_provides":
        print(f"    ? SUSPECT PROVIDES: '{issue['tag']}' — {issue['evidence'][0]}")
    elif issue["type"] == "missing_wants":
        print(f"    ⚠ MISSING WANTS: '{issue['tag']}' "
              f"({issue['neighbor_pct']:.0%} of neighbors have it)")
        print(f"      e.g. {', '.join(issue['evidence'][:3])}")


if __name__ == "__main__":
    main()
