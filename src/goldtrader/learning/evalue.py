"""Anytime-valid paired champion/challenger test: a betting e-process (test supermartingale).

Why (research/04_learning_and_validation.md §4.2, LEARNING_SYSTEM §3.3–§3.4): a fixed-horizon test
that is peeked at every day inflates false positives. Ramdas, Grünwald, Vovk & Shafer, "Game-
Theoretic Statistics and Safe Anytime-Valid Inference" (Statistical Science 38(4), 2023) show that
a nonnegative supermartingale W_t with W_0 = 1 under H0 satisfies Ville's inequality
P(sup_t W_t ≥ 1/α) ≤ α, so the rule "promote when W_t ≥ 20" has type-I error ≤ 0.05 *at any
stopping time* — the pipeline may look every day and stop whenever it likes.

Construction (Assumption, the §4.2 "bounded or winsorised statistic" recommendation):

    d_t      = challenger − champion per-trading-day net P&L (R or USD), paired on the same days
    c_t      = 3 · MAD_t         robust scale from the history d_1..d_{t−1} (MAD × 1.4826 ≈ σ)
    d̃_t      = clip(d_t, −c_t, +c_t)                 winsorised at ±3 MAD of the history so far
    x_t      = d̃_t / c_t  ∈ [−1, 1]                  sign-normalised observation
    λ_t      = clip(mean_{<t}(d) · c_t / var_{<t}(d), −cap, +cap)   capped Kelly bet from PAST data,
               0 for t < 5 (warm-up)
    W_t      = Π_{s≤t} (1 + max(λ_s, 0) · x_s)       e-process for H1: E[d] > 0 (promote)
    W'_t     = Π_{s≤t} (1 + max(−λ_s, 0) · (−x_s))   mirrored e-process for H1': E[d] < 0 (reject)

Validity: c_t and λ_t depend only on the past, so they are predictable; |x_t| ≤ 1 and 0 ≤ λ ≤ cap
≤ 1 keep every factor in [1 − cap, 1 + cap] > 0, so W_t ≥ 0; under H0: E[x_t | past] ≤ 0 each
factor has conditional expectation ≤ 1, hence W_t is a nonnegative supermartingale and Ville's
inequality applies. The bet is taken only in the hypothesised direction (positive part of λ): a
two-sided λ would make W a *sub*martingale under H0 when the champion is better, which is why the
two directions run as two one-sided processes. The cap (0.5) bounds the loss per step and
approximates the Kelly bet log(1 + λx) ≈ λx − λ²x²/2 maximised at λ ≈ E[x]/E[x²].
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

DAY_MS = 86_400_000
MAD_TO_SIGMA = 1.4826


class EProcess:
    """Paired e-process over daily differences `d_t = challenger − champion` (see module doc).

    `update(d)` ingests one day; `e_value` is W_t; `mirror_e_value` is W'_t; `decision()` is
    'promote' when W_t ≥ threshold, 'reject' when W'_t ≥ threshold, else 'continue'. A decision
    once reached is sticky (the pipeline stops at the first crossing, as Ville's inequality
    assumes a stopping rule, not a running maximum re-examined after the fact)."""

    def __init__(
        self,
        threshold: float = 20.0,
        cap: float = 0.5,
        *,
        warmup: int = 5,
        winsor_mads: float = 3.0,
    ) -> None:
        if threshold <= 1.0:
            raise ValueError("threshold must exceed 1 (α = 1/threshold)")
        if not 0.0 < cap <= 1.0:
            raise ValueError("cap must be in (0, 1]")
        self.threshold = float(threshold)
        self.cap = float(cap)
        self.warmup = int(warmup)
        self.winsor_mads = float(winsor_mads)
        self.history: list[float] = []
        self.log_wealth = 0.0
        self.log_wealth_mirror = 0.0
        self._decision: str = "continue"

    # --- state -----------------------------------------------------------------------------
    @property
    def n(self) -> int:
        return len(self.history)

    @property
    def e_value(self) -> float:
        return math.exp(self.log_wealth)

    @property
    def mirror_e_value(self) -> float:
        return math.exp(self.log_wealth_mirror)

    def decision(self) -> str:
        return self._decision

    def state(self) -> dict[str, Any]:
        """JSON-friendly snapshot for `experiments.stats_json`."""
        return {
            "n": self.n,
            "e_value": self.e_value,
            "mirror_e_value": self.mirror_e_value,
            "threshold": self.threshold,
            "cap": self.cap,
            "decision": self._decision,
        }

    # --- betting -------------------------------------------------------------------------------
    def _scale(self) -> float:
        """c_t = 3 × (1.4826 × MAD) of the history; falls back to the std, then mean |d|."""
        past = np.asarray(self.history, dtype=float)
        med = float(np.median(past))
        mad = float(np.median(np.abs(past - med))) * MAD_TO_SIGMA
        if mad <= 0.0:
            mad = float(past.std(ddof=0))
        if mad <= 0.0:
            mad = float(np.mean(np.abs(past)))
        return self.winsor_mads * mad

    def bet(self) -> float:
        """λ_t from the history so far: clip(mean · c_t / var, −cap, cap); 0 during warm-up or when
        the scale is degenerate (all past differences identical)."""
        if self.n < self.warmup:
            return 0.0
        past = np.asarray(self.history, dtype=float)
        c = self._scale()
        var = float(past.var(ddof=0))
        if c <= 0.0 or var <= 0.0:
            return 0.0
        lam = float(past.mean()) * c / var
        return float(np.clip(lam, -self.cap, self.cap))

    def update(self, d: float) -> str:
        """Ingest one paired daily difference and return the (sticky) decision."""
        d = float(d)
        if not math.isfinite(d):
            raise ValueError("difference must be finite")
        if self._decision == "continue" and self.n >= self.warmup:
            lam = self.bet()
            c = self._scale()
            if c > 0.0 and lam != 0.0:
                x = float(np.clip(d, -c, c)) / c  # winsorise, then normalise to [−1, 1]
                if lam > 0.0:
                    self.log_wealth += math.log1p(lam * x)
                else:
                    self.log_wealth_mirror += math.log1p((-lam) * (-x))
            if self.log_wealth >= math.log(self.threshold):
                self._decision = "promote"
            elif self.log_wealth_mirror >= math.log(self.threshold):
                self._decision = "reject"
        self.history.append(d)
        return self._decision

    def extend(self, diffs: Iterable[float]) -> str:
        for d in diffs:
            self.update(d)
        return self._decision

    @classmethod
    def from_pairs(
        cls, diffs: Iterable[float], threshold: float = 20.0, cap: float = 0.5, **kw: Any
    ) -> EProcess:
        """Replay a sequence of paired differences (deterministic; the pipeline recomputes the
        process from the journal on every advance rather than persisting it)."""
        ep = cls(threshold, cap, **kw)
        ep.extend(diffs)
        return ep


def _by_day(trades: Sequence[Mapping[str, Any]], key: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for t in trades:
        exit_ms = t.get("exit_ms")
        if exit_ms is None:
            continue
        v = t.get(key)
        if v is None:
            continue
        day = int(exit_ms) // DAY_MS
        out[day] = out.get(day, 0.0) + float(v)
    return out


def daily_pairs(
    trades_a: Sequence[Mapping[str, Any]],
    trades_b: Sequence[Mapping[str, Any]],
    key: str = "net_pnl",
) -> list[float]:
    """Per-trading-day differences `a − b` of summed `key` (``net_pnl`` USD or ``r_multiple``)
    over the union of days on which either side closed a trade, in day order. A side with no
    trade on a day contributes 0 (it was flat). Trades are dicts with `exit_ms` (journal
    `trades` / `shadow_trades` rows)."""
    a = _by_day(trades_a, key)
    b = _by_day(trades_b, key)
    return [a.get(day, 0.0) - b.get(day, 0.0) for day in sorted(set(a) | set(b))]
