"""Shared CLI display helpers for recommendation output."""

from urllib.parse import quote


def print_card_table(title: str, rows: list[dict], top_n: int = 50) -> None:
    """Print a ranked card table with OSC 8 clickable Scryfall links.

    Each row dict must have: name, type_line, cmc, score.
    """
    print(f"\n{'=' * 80}")
    print(title)
    print(f"{'=' * 80}")

    print(f"  {'#':>3s}  {'Name':<35s}  {'Type':<28s}  {'CMC':>3s}  {'Score':>6s}")
    print(f"  {'─' * 3}  {'─' * 35}  {'─' * 28}  {'─' * 3}  {'─' * 6}")

    for rank, row in enumerate(rows[:top_n], 1):
        name = row["name"]
        type_line = row.get("type_line", "")
        cmc = row.get("cmc", 0)
        score = row["score"]

        short_type = type_line.replace("Legendary ", "L ")
        if len(short_type) > 28:
            short_type = short_type[:27] + "\u2026"

        scryfall_url = f"https://scryfall.com/search?q=!%22{quote(name, safe='')}%22"
        osc_name = f"\033]8;;{scryfall_url}\033\\{name}\033]8;;\033\\"
        pad = max(0, 35 - len(name))
        padded_name = osc_name + " " * pad

        cmc_str = f"{cmc:3.0f}" if cmc == int(cmc) else f"{cmc:3.1f}"
        print(f"  {rank:3d}  {padded_name}  {short_type:<28s}  {cmc_str}  {score:6.1f}")
