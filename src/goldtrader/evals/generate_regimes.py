"""Deterministic generator for `sets/regimes_v1.jsonl` (D-040; LOCAL_LLM.md §7 "indicator table →
regime label").

Labels come straight from the thresholds in `goldtrader.data.features` (`ATR_PCT_CALM_DAILY`,
`ATR_PCT_STRESS_DAILY`): `atr_pct_daily < 1.2 %` → calm, `> 2.5 %` → stress, else normal, boundary
values included. The other features (rv_pctile, trend_state, spread_regime, hour_utc) are drawn
from a fixed-seed RNG so the model cannot label from them alone. `tests/test_evals_sets.py`
checks that `render()` reproduces the committed file byte for byte; regenerate with

    .venv/bin/python -m goldtrader.evals.generate_regimes
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from goldtrader.data.features import ATR_PCT_CALM_DAILY, ATR_PCT_STRESS_DAILY

SEED = 20261105
OUT_PATH = Path(__file__).resolve().parent / "sets" / "regimes_v1.jsonl"

ATR_PCT_DAILY_VALUES: tuple[float, ...] = (
    # calm (< 0.012): 13 values incl. one just under the threshold
    0.0040,
    0.0055,
    0.0062,
    0.0070,
    0.0078,
    0.0085,
    0.0091,
    0.0098,
    0.0104,
    0.0109,
    0.0113,
    0.0117,
    0.0119,
    # normal (0.012 .. 0.025 inclusive): 14 values incl. both boundaries
    0.0120,
    0.0126,
    0.0135,
    0.0148,
    0.0160,
    0.0172,
    0.0185,
    0.0199,
    0.0210,
    0.0222,
    0.0234,
    0.0243,
    0.0249,
    0.0250,
    # stress (> 0.025): 13 values incl. one just over the threshold
    0.0251,
    0.0258,
    0.0266,
    0.0279,
    0.0290,
    0.0305,
    0.0322,
    0.0340,
    0.0361,
    0.0385,
    0.0410,
    0.0440,
    0.0480,
)


def regime_label(atr_pct_daily: float) -> str:
    """The code regime for a daily-equivalent ATR fraction (same comparisons as `add_features`)."""
    if atr_pct_daily < ATR_PCT_CALM_DAILY:
        return "calm"
    if atr_pct_daily > ATR_PCT_STRESS_DAILY:
        return "stress"
    return "normal"


def generate() -> list[dict[str, Any]]:
    """The 40 seed items, in a fixed order, with labels from `regime_label`."""
    rng = random.Random(SEED)
    items: list[dict[str, Any]] = []
    for i, atr in enumerate(ATR_PCT_DAILY_VALUES, start=1):
        features = {
            "atr_pct_daily": atr,
            "rv_pctile": round(rng.uniform(0.0, 1.0), 2),
            "trend_state": rng.choice([-1, 0, 1]),
            "spread_regime": rng.choice(["normal", "normal", "wide", "unknown"]),
            "hour_utc": rng.randrange(0, 24),
        }
        items.append({"id": f"r{i:03d}", "features": features, "label": regime_label(atr)})
    return items


def render(items: list[dict[str, Any]] | None = None) -> str:
    """JSONL text (one compact object per line, trailing newline) for `items` or `generate()`."""
    rows = generate() if items is None else items
    return "".join(json.dumps(r, separators=(", ", ": ")) + "\n" for r in rows)


def main(path: Path = OUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(), encoding="utf-8", newline="\n")
    return path


if __name__ == "__main__":  # pragma: no cover
    print(main())
