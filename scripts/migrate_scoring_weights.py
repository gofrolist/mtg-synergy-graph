"""One-shot extractor: read current _RULE_QUALITY_MULTIPLIER and
_FLAT_WEIGHT_OVERRIDES from universal_scorer.py and emit
data/scoring_weights.json with empty comments.

Asserts float round-trip identity before writing: every value loaded
back from the emitted JSON must repr() identically to the source dict
value, otherwise compute_config_hash would drift.

Deleted in a follow-up commit once the migration lands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mtg_synergy_graph.universal_scorer import (
    _FLAT_WEIGHT_OVERRIDES,
    _RULE_QUALITY_MULTIPLIER,
)


def main() -> int:
    payload = {
        "rule_quality_multiplier": {
            k: {"value": v, "comment": ""} for k, v in sorted(_RULE_QUALITY_MULTIPLIER.items())
        },
        "flat_weight_overrides": {k: {"value": v, "comment": ""} for k, v in sorted(_FLAT_WEIGHT_OVERRIDES.items())},
    }
    out_path = Path("data/scoring_weights.json")
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    reloaded = json.loads(out_path.read_text(encoding="utf-8"))
    for section, source in (
        ("rule_quality_multiplier", _RULE_QUALITY_MULTIPLIER),
        ("flat_weight_overrides", _FLAT_WEIGHT_OVERRIDES),
    ):
        for k, v in source.items():
            roundtripped = reloaded[section][k]["value"]
            if repr(float(roundtripped)) != repr(v):
                print(
                    f"FLOAT DRIFT: {section}.{k}: {v!r} -> {roundtripped!r}",
                    file=sys.stderr,
                )
                return 1
    print(
        f"wrote {out_path} ({len(_RULE_QUALITY_MULTIPLIER)} multipliers, {len(_FLAT_WEIGHT_OVERRIDES)} flat overrides)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
