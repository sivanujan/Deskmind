"""Feature data-quality gate.

After every feature build, call ``check_feature_quality`` to detect:
1. Any feature that is 100 % null across all rows.
2. Any feature that has exactly one distinct non-null value (constant).

Either condition means the feature will contribute nothing to any model and
almost certainly indicates a bug in the builder or the upstream agent state.
The gate raises ``FeatureQualityError`` with a human-readable table of bad features.

Usage::

    from deskmind.features.quality import check_feature_quality
    rows = [row.as_dict() for row in feature_rows]
    check_feature_quality(rows)  # raises on bad features

The check is intentionally cheap — O(n * f) with no allocations beyond counts.
"""

from __future__ import annotations

from typing import Any, Sequence


class FeatureQualityError(Exception):
    """Raised when feature rows fail the data-quality check."""


# Features that are legitimately allowed to be null (not yet available on warm-up):
#   dist_to_pdh_atr / dist_to_pdl_atr — only available after the first full trading day
#   position_pct_1h — only available after the first BOS forms a PD range
_NULLABLE_FEATURES: frozenset[str] = frozenset({
    "dist_to_pdh_atr",
    "dist_to_pdl_atr",
    "position_pct_1h",
})

# Metadata / identifier columns — these may be constant by design (e.g. single symbol build)
_IDENTITY_COLUMNS: frozenset[str] = frozenset({
    "symbol",
    "universe_member",
    "weekend_flag",
})


def check_feature_quality(
    rows: Sequence[dict[str, Any]],
    *,
    allow_nullable: frozenset[str] = _NULLABLE_FEATURES,
    allow_constant: frozenset[str] = _IDENTITY_COLUMNS,
    min_rows: int = 10,
) -> None:
    """Validate feature rows for data-quality problems.

    Args:
        rows: Sequence of feature row dicts (e.g., from ``FeatureRow.as_dict()``).
        allow_nullable: Feature names that are allowed to be 100 % null.
        allow_constant: Feature names that are allowed to be constant.
        min_rows: Minimum number of rows before the check is meaningful (default 10).

    Raises:
        FeatureQualityError: If any non-exempt feature is 100 % null or constant.
        ValueError: If ``rows`` is empty.
    """
    if not rows:
        raise ValueError("check_feature_quality: rows is empty — nothing to check")

    if len(rows) < min_rows:
        # Not enough rows for a meaningful quality signal; skip.
        return

    all_keys: list[str] = [k for k in rows[0] if k not in ("timestamp",)]
    n = len(rows)

    all_null: list[str] = []
    constant: list[str] = []

    for key in all_keys:
        values = [row.get(key) for row in rows]
        non_null = [v for v in values if v is not None]

        if len(non_null) == 0:
            # 100 % null
            if key not in allow_nullable:
                all_null.append(key)
            continue

        null_pct = round((n - len(non_null)) / n * 100, 1)
        distinct = set(non_null)
        if len(distinct) == 1 and null_pct == 0.0 and key not in allow_constant:
            constant.append(f"{key}={next(iter(distinct))!r}")

    problems: list[str] = []
    if all_null:
        problems.append(
            "100% NULL features (bug — check builder key paths):\n"
            + "\n".join(f"  - {k}" for k in sorted(all_null))
        )
    if constant:
        problems.append(
            f"Constant-value features (bug — check builder logic or agent warm-up):\n"
            + "\n".join(f"  - {v}" for v in sorted(constant))
        )

    if problems:
        raise FeatureQualityError(
            f"Feature quality check FAILED on {n} rows:\n\n"
            + "\n\n".join(problems)
            + "\n\nFix the builder or agent state before writing features."
        )


def feature_quality_report(
    rows: Sequence[dict[str, Any]],
    *,
    allow_nullable: frozenset[str] = _NULLABLE_FEATURES,
) -> list[dict[str, Any]]:
    """Return a per-feature statistics table (for logging / pilot report).

    Each entry has: feature, null_pct, distinct_count, sample_values.
    Does not raise; use ``check_feature_quality`` for the strict gate.
    """
    if not rows:
        return []

    n = len(rows)
    all_keys = [k for k in rows[0] if k != "timestamp"]
    report = []

    for key in all_keys:
        values = [row.get(key) for row in rows]
        non_null = [v for v in values if v is not None]
        null_pct = round((n - len(non_null)) / n * 100, 1)
        distinct = sorted({str(v) for v in non_null})[:5]

        report.append({
            "feature": key,
            "null_pct": null_pct,
            "distinct_count": len(set(str(v) for v in non_null)),
            "sample_values": distinct,
            "allowed_null": key in allow_nullable,
        })

    return sorted(report, key=lambda r: (-r["null_pct"], r["feature"]))
