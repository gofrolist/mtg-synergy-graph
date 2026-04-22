"""Rule-pair collinearity diagnostic via Pearson + VIF (Unit 6).

Builds a ``rules × (commander, candidate)`` activation matrix from the
persisted tensor, computes Pearson correlation + variance inflation
factor (VIF) between every pair of rules, and flags pairs above
thresholds as collinearity candidates. No new dependencies — numpy is
already in the project (used by the IDF computation in
``universal_scorer``).

VIF = diagonal of ``inv(corr_matrix)``. A rule whose activations are
perfectly collinear with another rule has VIF → ∞; in practice ``VIF >
5`` is a reasonable "likely redundant" threshold. Pearson |r| > 0.8
catches the same class of cases but is easier to interpret.

Rules whose activation vector is all-zero or constant are dropped with
a warning — they have no signal to correlate against.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from mtg_synergy_graph.bench.tensor import compute_config_hash

#: Rules with VIF above this are flagged. 5 is a conservative threshold
#: widely used in regression diagnostics; 10 is "serious concern."
_VIF_THRESHOLD = 5.0

#: Pearson |r| above this is also flagged. 0.8 is the common "strong
#: correlation" threshold.
_PEARSON_THRESHOLD = 0.8


@dataclass(frozen=True)
class CollinearPair:
    """One flagged rule pair."""

    rule_a: str
    rule_b: str
    pearson_r: float
    vif_a: float
    vif_b: float


@dataclass(frozen=True)
class CollinearityReport:
    """Output of ``bench.py audit --collinearity``."""

    config_hash: str
    rules_examined: int
    rules_dropped: tuple[str, ...]
    pairs_flagged: tuple[CollinearPair, ...]
    vif_per_rule: dict[str, float]


def compute_collinearity(
    conn: sqlite3.Connection,
    config_hash: str | None = None,
    pearson_threshold: float = _PEARSON_THRESHOLD,
    vif_threshold: float = _VIF_THRESHOLD,
) -> CollinearityReport:
    """Build the activation matrix and flag likely-collinear rule pairs."""
    h = config_hash or compute_config_hash()
    rows = conn.execute(
        """
        SELECT rule_id, commander, candidate, contribution
        FROM rule_contributions
        WHERE config_hash = ?
        """,
        (h,),
    ).fetchall()

    if not rows:
        return CollinearityReport(
            config_hash=h,
            rules_examined=0,
            rules_dropped=(),
            pairs_flagged=(),
            vif_per_rule={},
        )

    # Build a (rule_id, cmdr_cand_idx) sparse map; densify into matrix.
    rule_ids = sorted({r["rule_id"] for r in rows})
    pair_keys = sorted({(r["commander"], r["candidate"]) for r in rows})
    rule_idx = {r: i for i, r in enumerate(rule_ids)}
    pair_idx = {(c, k): i for i, (c, k) in enumerate(pair_keys)}

    activations = np.zeros((len(rule_ids), len(pair_keys)), dtype=np.float64)
    for r in rows:
        activations[
            rule_idx[r["rule_id"]],
            pair_idx[(r["commander"], r["candidate"])],
        ] = r["contribution"]

    # Drop rules with zero variance (would produce NaN in corr_matrix).
    variances = activations.var(axis=1)
    keep_mask = variances > 0
    dropped = tuple(rule for rule, keep in zip(rule_ids, keep_mask, strict=True) if not keep)
    kept_rule_ids = [rule for rule, keep in zip(rule_ids, keep_mask, strict=True) if keep]
    kept_activations = activations[keep_mask]

    if kept_activations.shape[0] < 2:
        return CollinearityReport(
            config_hash=h,
            rules_examined=len(rule_ids) - len(dropped),
            rules_dropped=dropped,
            pairs_flagged=(),
            vif_per_rule={},
        )

    # np.corrcoef on a 2D matrix always returns a 2D matrix; the
    # | float64 branch in its return type is for the 1D input case.
    corr_any = np.corrcoef(kept_activations)
    corr = np.atleast_2d(np.nan_to_num(np.asarray(corr_any), nan=0.0))
    try:
        vif_vec = np.diag(np.linalg.pinv(corr))
    except np.linalg.LinAlgError:
        vif_vec = np.full(len(kept_rule_ids), float("nan"))

    vif_per_rule = {rule: float(vif_vec[i]) for i, rule in enumerate(kept_rule_ids)}

    pairs: list[CollinearPair] = []
    for i, rule_a in enumerate(kept_rule_ids):
        for j in range(i + 1, len(kept_rule_ids)):
            rule_b = kept_rule_ids[j]
            r_val = float(corr[i, j])
            vif_a = float(vif_vec[i])
            vif_b = float(vif_vec[j])
            flagged_by_vif = vif_a > vif_threshold and vif_b > vif_threshold
            flagged_by_r = abs(r_val) > pearson_threshold
            if flagged_by_vif or flagged_by_r:
                pairs.append(
                    CollinearPair(
                        rule_a=rule_a,
                        rule_b=rule_b,
                        pearson_r=r_val,
                        vif_a=vif_a,
                        vif_b=vif_b,
                    )
                )

    # Sort by |r| desc so the strongest correlations lead output.
    pairs.sort(key=lambda p: abs(p.pearson_r), reverse=True)

    return CollinearityReport(
        config_hash=h,
        rules_examined=len(kept_rule_ids),
        rules_dropped=dropped,
        pairs_flagged=tuple(pairs),
        vif_per_rule=vif_per_rule,
    )
