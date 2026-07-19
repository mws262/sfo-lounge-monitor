"""Composite 'how hard is it to get through SFO right now' scorer.

Each component contributes a 0..100 sub-score (100 == worst). Weights below are
starting points -- tune against logged history. The lounge is deliberately NOT
part of this score: it's a separate question, reported alongside.

Missing signals (no creds, endpoint down) are dropped and the remaining weights
renormalized, so the headline is always over whatever we could actually read.
"""
from __future__ import annotations

from typing import Any

# component key -> (weight, label). Security replaces the dead TSA feed.
WEIGHTS = {
    "security": (0.35, "security"),
    "fog": (0.20, "fog"),
    "departures": (0.20, "departures"),
    "gdp": (0.15, "ground-delay"),
    "approach": (0.10, "approach"),
    "drive": (0.10, "drive"),
}


def composite(subscores: dict[str, float | None]) -> dict[str, Any]:
    """Combine present sub-scores into a headline 0..100 + breakdown.

    `subscores` maps component key -> 0..100 or None (unavailable).
    """
    present = {
        k: v for k, v in subscores.items()
        if v is not None and k in WEIGHTS
    }
    total_w = sum(WEIGHTS[k][0] for k in present)
    if not present or total_w == 0:
        return {"score": None, "components": {}, "missing": list(subscores)}

    headline = sum(v * WEIGHTS[k][0] for k, v in present.items()) / total_w
    components = {
        k: {"score": round(v, 1),
            "weight": round(WEIGHTS[k][0] / total_w, 3),
            "label": WEIGHTS[k][1]}
        for k, v in present.items()
    }
    missing = [k for k in subscores if k not in present]
    return {"score": round(headline, 1), "components": components,
            "missing": missing}


def band(score: float | None) -> str:
    """Coarse human band for a headline score."""
    if score is None:
        return "unknown"
    if score < 20:
        return "quiet"
    if score < 40:
        return "light"
    if score < 60:
        return "moderate"
    if score < 80:
        return "busy"
    return "rough"
