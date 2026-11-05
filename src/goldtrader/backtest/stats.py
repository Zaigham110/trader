"""Strategy-validation statistics for the offline gate and the production monitors.

Pure numpy/pandas (no scipy). Everything that draws random numbers takes an explicit
``rng: numpy.random.Generator`` so results are reproducible.

Sources (see docs/research/04_learning_and_validation.md for the verification status):

- Lo, "The Statistics of Sharpe Ratios", FAJ 58(4), 2002, eq. (9): IID standard error of the SR.
- Bailey, Borwein, López de Prado, Zhu, "Pseudo-Mathematics and Financial Charlatanism",
  Notices AMS 2014: E[max SR] (eq. 2.4) and Minimum Backtest Length (Theorem 3.1, eq. 3.2).
- Bailey & López de Prado, "The Deflated Sharpe Ratio", JPM 40(5), 2014: DSR / PSR.
- Bailey, Borwein, López de Prado, Zhu, "The Probability of Backtest Overfitting",
  J. Comp. Finance 20(4), 2017: CSCV procedure (steps a–g, section 2.4).
- Harvey & Liu, "Backtesting", JPM 41(1), 2015: haircut Sharpe ratio and the
  Bonferroni / Holm / BHY adjustments; Benjamini & Hochberg 1995 for BH.
- López de Prado, *Advances in Financial Machine Learning*, 2018, ch. 7 (purging, embargo)
  and ch. 12 (combinatorial purged cross-validation, backtest paths).
- Page, "Continuous inspection schemes", Biometrika 41, 1954 (CUSUM).
- Masters, *Permutation and Randomization Tests for Trading System Development*, 2020
  (random-timing permutation, trade-order Monte Carlo).

Thresholds quoted in the docstrings are the defaults of docs/LEARNING_SYSTEM.md §3.2 and §4.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from math import comb

import numpy as np
from numpy.typing import ArrayLike, NDArray

EULER_GAMMA = 0.5772156649015329
"""Euler–Mascheroni constant (Bailey et al. 2014 eq. 2.4)."""

FloatArray = NDArray[np.float64]

__all__ = [
    "EULER_GAMMA",
    "BootstrapCI",
    "CPCVSplit",
    "CusumResult",
    "HaircutResult",
    "MCDDResult",
    "PBOResult",
    "PChartResult",
    "PermResult",
    "RollingSharpeResult",
    "SharpeCI",
    "assemble_cpcv_paths",
    "block_bootstrap_ci",
    "bootstrap_ci",
    "cpcv_paths",
    "cpcv_splits",
    "cusum",
    "deflated_sharpe",
    "expected_max_sharpe",
    "haircut_sharpe",
    "hit_rate_pchart",
    "max_drawdown",
    "max_trials_for_backtest_length",
    "min_backtest_length_years",
    "min_trades_for_sharpe",
    "monte_carlo_drawdown",
    "multiple_testing_adjust",
    "normal_cdf",
    "normal_ppf",
    "pbo_cscv",
    "permutation_test_random_timing",
    "probabilistic_sharpe",
    "rolling_sharpe_bands",
    "sharpe",
    "sharpe_ci",
    "sharpe_se",
    "skew_kurt",
]


# --------------------------------------------------------------------------------------
# Normal distribution helpers (no scipy)
# --------------------------------------------------------------------------------------

_erf = np.vectorize(math.erf, otypes=[np.float64])


def normal_cdf(x: ArrayLike) -> FloatArray | float:
    """Standard normal CDF Phi(x) = 0.5 * (1 + erf(x / sqrt(2))), via ``math.erf``."""
    arr = np.asarray(x, dtype=np.float64)
    out = 0.5 * (1.0 + _erf(arr / math.sqrt(2.0)))
    return float(out) if out.ndim == 0 else out


# Acklam's rational approximation coefficients (relative error ~1.15e-9 before refinement).
_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_P_LOW = 0.02425
_P_HIGH = 1.0 - _P_LOW


def _ppf_scalar(p: float) -> float:
    if math.isnan(p) or p < 0.0 or p > 1.0:
        return math.nan
    if p == 0.0:
        return -math.inf
    if p == 1.0:
        return math.inf
    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0
        )
    elif p <= _P_HIGH:
        q = p - 0.5
        r = q * q
        x = (
            (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5])
            * q
            / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
        )
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0
        )
    # One Halley refinement step using the erf-based CDF (Acklam's suggested refinement).
    e = 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(0.5 * x * x)
    x = x - u / (1.0 + 0.5 * x * u)
    return x


_ppf_vec = np.vectorize(_ppf_scalar, otypes=[np.float64])


def normal_ppf(p: ArrayLike) -> FloatArray | float:
    """Inverse standard normal CDF (quantile function) Z^-1(p).

    Acklam's rational approximation refined with one Halley step on the erf-based CDF; absolute
    error below 1e-12 on (1e-300, 1 - 1e-16).
    """
    arr = np.asarray(p, dtype=np.float64)
    out = _ppf_vec(arr)
    return float(out) if out.ndim == 0 else out


def _as_1d(x: ArrayLike, name: str = "values") -> FloatArray:
    arr = np.asarray(x, dtype=np.float64).ravel()
    if arr.size == 0:
        raise ValueError(f"{name} must not be empty")
    return arr


def _check_rng(rng: np.random.Generator | None) -> np.random.Generator:
    if rng is None:
        raise ValueError("pass an explicit rng=numpy.random.default_rng(seed) for reproducibility")
    return rng


# --------------------------------------------------------------------------------------
# 1. Sharpe ratio, its standard error and the minimum number of trades
# --------------------------------------------------------------------------------------


def sharpe(returns: ArrayLike, periods_per_year: float = 1.0) -> float:
    """Annualised Sharpe ratio: mean(r) / std(r, ddof=1) * sqrt(periods_per_year).

    Excess returns are assumed (risk-free already subtracted or zero). With fewer than two
    observations or zero standard deviation the function returns 0.0, so it can serve as a
    ranking metric for degenerate trials. The sqrt(periods) annualisation is exact only under
    IID returns (Lo 2002, "Time Aggregation" section).
    """
    r = np.asarray(returns, dtype=np.float64).ravel()
    if r.size < 2:
        return 0.0
    sd = r.std(ddof=1)
    if not np.isfinite(sd) or sd == 0.0:
        return 0.0
    return float(r.mean() / sd * math.sqrt(periods_per_year))


def sharpe_se(sr: float, n: int) -> float:
    """Standard error of an IID Sharpe estimate (Lo 2002, eq. 9).

    SE(SR_hat) = sqrt((1 + 0.5 * SR^2) / n), with SR and n in the same frequency (a per-trade SR
    with n trades, a monthly SR with n months). Annualise the SE with the same sqrt(k) factor as
    the SR.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    return math.sqrt((1.0 + 0.5 * sr * sr) / n)


@dataclass(frozen=True)
class SharpeCI:
    """Annualised Sharpe estimate with its Lo (2002) IID confidence interval."""

    sr: float
    lower: float
    upper: float
    se: float
    n: int
    alpha: float


def sharpe_ci(returns: ArrayLike, periods_per_year: float = 1.0, alpha: float = 0.05) -> SharpeCI:
    """Two-sided (1 - alpha) confidence interval for the annualised SR (Lo 2002, eq. 9).

    The SE is computed at the observation frequency, sqrt((1 + 0.5 SR_p^2) / n), then both the
    estimate and the interval are scaled by sqrt(periods_per_year).
    """
    r = _as_1d(returns, "returns")
    if r.size < 2:
        raise ValueError("need at least two returns")
    sr_p = sharpe(r, 1.0)
    se_p = sharpe_se(sr_p, r.size)
    z = float(normal_ppf(1.0 - alpha / 2.0))
    scale = math.sqrt(periods_per_year)
    return SharpeCI(
        sr=sr_p * scale,
        lower=(sr_p - z * se_p) * scale,
        upper=(sr_p + z * se_p) * scale,
        se=se_p * scale,
        n=int(r.size),
        alpha=alpha,
    )


def min_trades_for_sharpe(
    annual_sr: float, trades_per_year: float, alpha: float = 0.05, one_sided: bool = True
) -> int:
    """Trades needed to show a positive per-trade Sharpe at level alpha (rule of thumb).

    From Lo's SE: SR_t / SE > z  <=>  n > (z / SR_t)^2 (1 + 0.5 SR_t^2), with
    SR_t = annual_sr / sqrt(trades_per_year) and z = Z^-1(1 - alpha) (one-sided) or
    Z^-1(1 - alpha/2). docs/research/04 §7: annual SR 1.0 at 250 trades/year -> about 680 trades.
    """
    if annual_sr <= 0 or trades_per_year <= 0:
        raise ValueError("annual_sr and trades_per_year must be positive")
    sr_t = annual_sr / math.sqrt(trades_per_year)
    z = float(normal_ppf(1.0 - alpha)) if one_sided else float(normal_ppf(1.0 - alpha / 2.0))
    n = (z / sr_t) ** 2 * (1.0 + 0.5 * sr_t * sr_t)
    return math.ceil(n)


# --------------------------------------------------------------------------------------
# 2. Expected maximum Sharpe, Deflated Sharpe Ratio, Minimum Backtest Length
# --------------------------------------------------------------------------------------


def expected_max_sharpe(n_trials: int, var_sr: float = 1.0) -> float:
    """Expected maximum of N independent trial Sharpe ratios under a zero-true-SR null.

    Bailey, Borwein, López de Prado, Zhu (2014), eq. (2.4) / (3.1):
        E[max_N] ~= sqrt(V[SR]) * ((1 - g) Z^-1(1 - 1/N) + g Z^-1(1 - 1/(N e))),  g = 0.5772...
    valid for N >> 1, upper bound sqrt(V[SR] * 2 ln N). For N <= 1 the function returns 0.0
    (the expected value of a single centred draw). ``var_sr`` is the variance of the trials'
    SR estimates, in the same (per-period or annual) units as the SR you compare against.
    """
    if n_trials <= 1:
        return 0.0
    if var_sr < 0:
        raise ValueError("var_sr must be non-negative")
    n = float(n_trials)
    z1 = float(normal_ppf(1.0 - 1.0 / n))
    z2 = float(normal_ppf(1.0 - 1.0 / (n * math.e)))
    return math.sqrt(var_sr) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def probabilistic_sharpe(
    sr_hat: float, sr_benchmark: float, n_obs: int, skew: float = 0.0, kurt: float = 3.0
) -> float:
    """Probabilistic Sharpe Ratio PSR(SR*) = Phi[(SR_hat - SR*) sqrt(T-1) / sqrt(1 - g3 SR_hat
    + (g4 - 1)/4 SR_hat^2)] (Bailey & López de Prado 2012/2014).

    SRs are per-observation (NOT annualised); ``kurt`` is the non-excess kurtosis (normal = 3).
    """
    if n_obs < 2:
        raise ValueError("n_obs must be at least 2")
    denom_sq = 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat * sr_hat
    if denom_sq <= 0:
        raise ValueError("non-positive variance term; check skew/kurt/sr units (per-period SR)")
    stat = (sr_hat - sr_benchmark) * math.sqrt(n_obs - 1.0) / math.sqrt(denom_sq)
    return float(normal_cdf(stat))


def deflated_sharpe(
    sr_hat: float, n_trials: int, var_sr: float, n_obs: int, skew: float = 0.0, kurt: float = 3.0
) -> float:
    """Deflated Sharpe Ratio (Bailey & López de Prado, JPM 2014).

    DSR = Phi[ (SR_hat - SR*) sqrt(T - 1) / sqrt(1 - g3 SR_hat + (g4 - 1)/4 SR_hat^2) ],
    SR* = expected_max_sharpe(n_trials, var_sr), g3 = skewness, g4 = (non-excess) kurtosis of
    the selected strategy's returns, T = n_obs. All SRs are per-observation, not annualised.

    The E[max] part is Verified against the Charlatanism paper (eq. 2.4). The DSR/PSR expression
    itself is **recalled from the JPM paper and labelled Assumption** in
    docs/research/04_learning_and_validation.md §1.3 (not re-verified against the JPM text here).
    Gate (LEARNING_SYSTEM.md §3.2): DSR >= 0.95, with N = all ledger rows since the last
    quarterly reset and V[SR] their SR variance.
    """
    sr_star = expected_max_sharpe(n_trials, var_sr)
    return probabilistic_sharpe(sr_hat, sr_star, n_obs, skew, kurt)


def min_backtest_length_years(
    n_trials: int, target_max_sr: float = 1.0, upper_bound: bool = False
) -> float:
    """Minimum Backtest Length in years (Bailey et al. 2014, Theorem 3.1, eq. 3.2).

    MinBTL ~= [ (1-g) Z^-1(1 - 1/N) + g Z^-1(1 - 1/(N e)) ]^2 / E[max_N]^2  <  2 ln N / E[max_N]^2,
    the years of backtest needed so that N skill-less trials are expected to produce an in-sample
    *annualised* SR no greater than ``target_max_sr``. ``upper_bound=True`` returns the
    2 ln N / target^2 bound instead. Paper's example (exact form): with 5 years of data, at most
    ~45 independent trials at E[max] = 1. Necessary, not sufficient (the paper's own caveat).
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if target_max_sr <= 0:
        raise ValueError("target_max_sr must be positive")
    if n_trials == 1:
        return 0.0
    if upper_bound:
        return 2.0 * math.log(n_trials) / target_max_sr**2
    return (expected_max_sharpe(n_trials, 1.0) / target_max_sr) ** 2


def max_trials_for_backtest_length(
    years: float, target_max_sr: float = 1.0, upper_bound: bool = False
) -> int:
    """Largest N whose MinBTL (exact form by default) does not exceed ``years``.

    Inverse of :func:`min_backtest_length_years`; 5 years at target 1.0 -> 45 (paper's example).
    """
    if years <= 0:
        raise ValueError("years must be positive")
    n = 1
    while min_backtest_length_years(n + 1, target_max_sr, upper_bound) <= years:
        n += 1
        if n > 10_000_000:
            break
    return n


def skew_kurt(returns: ArrayLike) -> tuple[float, float]:
    """Sample skewness m3 / m2^1.5 and non-excess kurtosis m4 / m2^2 (normal -> 3.0).

    Biased central-moment estimators (as used in the PSR/DSR papers); needs >= 3 observations.
    """
    r = _as_1d(returns, "returns")
    if r.size < 3:
        raise ValueError("need at least three observations")
    d = r - r.mean()
    m2 = float(np.mean(d * d))
    if m2 == 0.0:
        return 0.0, 3.0
    m3 = float(np.mean(d**3))
    m4 = float(np.mean(d**4))
    return m3 / m2**1.5, m4 / (m2 * m2)


# --------------------------------------------------------------------------------------
# 3. Probability of Backtest Overfitting via CSCV
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PBOResult:
    """Output of :func:`pbo_cscv`.

    ``pbo``: share of combinations whose logit <= 0. ``logits``: one per combination.
    ``is_metric`` / ``oos_metric``: the IS-best trial's IS and OOS metric per combination (the
    pairs used for the degradation regression OOS = intercept + slope * IS). ``prob_oos_loss``:
    share of combinations where the selected trial's OOS metric is negative.
    """

    pbo: float
    logits: FloatArray
    oos_degradation_slope: float
    oos_degradation_intercept: float
    prob_oos_loss: float
    n_combinations: int
    is_metric: FloatArray
    oos_metric: FloatArray
    selected: NDArray[np.int64]
    n_trials: int
    n_rows_used: int
    sampled: bool


def _rank_ascending(x: FloatArray) -> NDArray[np.int64]:
    """Ranks 1..N with the best (largest) value getting rank N; ties broken by position."""
    order = np.argsort(x, kind="stable")
    ranks = np.empty(x.size, dtype=np.int64)
    ranks[order] = np.arange(1, x.size + 1)
    return ranks


def _column_sharpe(m: FloatArray) -> FloatArray:
    """Per-column mean/std(ddof=1); columns with zero variance get 0."""
    sd = m.std(axis=0, ddof=1)
    mu = m.mean(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(sd > 0, mu / sd, 0.0)
    return np.asarray(out, dtype=np.float64)


def pbo_cscv(
    returns_matrix: ArrayLike,
    s: int = 16,
    metric: Callable[[FloatArray], FloatArray] | None = None,
    max_combinations: int = 12870,
    rng: np.random.Generator | None = None,
) -> PBOResult:
    """Probability of Backtest Overfitting by Combinatorially Symmetric Cross-Validation.

    Bailey, Borwein, López de Prado, Zhu (2017), section 2.4 (steps a–g):
    1. M is a T x N matrix of returns (rows = time, columns = trial configurations).
    2. Split the rows into S (even) contiguous sub-matrices of equal size; leftover rows
       (T mod S, the oldest ones) are dropped so every block has the same length.
    3. For each of the C(S, S/2) combinations take those S/2 blocks (in original order) as the
       training set J and the complement as the test set.
    4. Compute the metric per trial IS and OOS; rank the N trials (rank N = best); n* = IS-best;
       w = OOS rank of n* / (N + 1); logit = ln(w / (1 - w)).
    5. PBO = share of combinations with logit <= 0 (n* at or below the OOS median).
    Also reported: performance degradation (OLS of the selected trial's OOS metric on its IS
    metric; a negative slope is typical of overfitting) and the OOS probability of loss.

    ``metric`` maps a (rows x N) matrix to an N-vector; the default is the per-column
    mean/std Sharpe-like ratio, computed from per-block sufficient statistics (sum, sum of
    squares) so the default run over all 12,870 combinations at S = 16 is fast. The block
    order does not matter for the SR (paper's remark 2.4 b) but would for path-dependent
    metrics such as drawdown. If C(S, S/2) > ``max_combinations`` the combinations are sampled
    (with replacement, ``rng`` required) and ``sampled`` is True.
    Gate (LEARNING_SYSTEM.md §3.2): PBO <= 0.05.
    """
    m = np.asarray(returns_matrix, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError("returns_matrix must be 2-D (T rows x N trials)")
    if s < 2 or s % 2 != 0:
        raise ValueError("s must be an even integer >= 2")
    t_total, n = m.shape
    if n < 2:
        raise ValueError("need at least two trials")
    block_len = t_total // s
    if block_len < 2:
        raise ValueError(f"T={t_total} too short for s={s} blocks of >= 2 rows")
    t_used = block_len * s
    m = m[t_total - t_used :]  # drop the oldest leftover rows
    blocks = m.reshape(s, block_len, n)

    half = s // 2
    n_all = comb(s, half)
    sampled = False
    if n_all <= max_combinations:
        combos = list(combinations(range(s), half))
    else:
        gen = _check_rng(rng)
        sampled = True
        combos = [
            tuple(sorted(gen.choice(s, size=half, replace=False).tolist()))
            for _ in range(max_combinations)
        ]
    n_c = len(combos)
    all_idx = np.arange(s)

    is_vals = np.empty((n_c, n))
    oos_vals = np.empty((n_c, n))
    if metric is None:
        bsum = blocks.sum(axis=1)  # (S, N)
        bsq = (blocks * blocks).sum(axis=1)
        cnt = float(block_len * half)
        for i, c in enumerate(combos):
            idx = np.asarray(c)
            mask = np.ones(s, dtype=bool)
            mask[idx] = False
            for arr, sel in ((is_vals, idx), (oos_vals, all_idx[mask])):
                ssum = bsum[sel].sum(axis=0)
                ssq = bsq[sel].sum(axis=0)
                mu = ssum / cnt
                var = (ssq - cnt * mu * mu) / (cnt - 1.0)
                var = np.maximum(var, 0.0)
                sd = np.sqrt(var)
                with np.errstate(divide="ignore", invalid="ignore"):
                    arr[i] = np.where(sd > 0, mu / sd, 0.0)
    else:
        for i, c in enumerate(combos):
            idx = np.asarray(c)
            mask = np.ones(s, dtype=bool)
            mask[idx] = False
            j_is = blocks[idx].reshape(-1, n)
            j_oos = blocks[mask].reshape(-1, n)
            is_vals[i] = np.asarray(metric(j_is), dtype=np.float64)
            oos_vals[i] = np.asarray(metric(j_oos), dtype=np.float64)

    selected = np.empty(n_c, dtype=np.int64)
    logits = np.empty(n_c)
    sel_is = np.empty(n_c)
    sel_oos = np.empty(n_c)
    for i in range(n_c):
        is_rank = _rank_ascending(is_vals[i])
        oos_rank = _rank_ascending(oos_vals[i])
        n_star = int(np.argmax(is_rank))
        w = oos_rank[n_star] / (n + 1.0)
        selected[i] = n_star
        logits[i] = math.log(w / (1.0 - w))
        sel_is[i] = is_vals[i, n_star]
        sel_oos[i] = oos_vals[i, n_star]

    pbo = float(np.mean(logits <= 0.0))
    if np.ptp(sel_is) > 0:
        slope, intercept = np.polyfit(sel_is, sel_oos, 1)
    else:
        slope, intercept = 0.0, float(np.mean(sel_oos))
    return PBOResult(
        pbo=pbo,
        logits=logits,
        oos_degradation_slope=float(slope),
        oos_degradation_intercept=float(intercept),
        prob_oos_loss=float(np.mean(sel_oos < 0.0)),
        n_combinations=n_c,
        is_metric=sel_is,
        oos_metric=sel_oos,
        selected=selected,
        n_trials=n,
        n_rows_used=t_used,
        sampled=sampled,
    )


# --------------------------------------------------------------------------------------
# 4. Combinatorial purged cross-validation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CPCVSplit:
    """One CPCV split: ``test_groups`` are tested, ``path_ids[j]`` is the backtest path that the
    predictions for ``test_groups[j]`` belong to (see :func:`assemble_cpcv_paths`)."""

    train_idx: NDArray[np.int64]
    test_idx: NDArray[np.int64]
    test_groups: tuple[int, ...]
    path_ids: tuple[int, ...]
    group_bounds: tuple[tuple[int, int], ...] = field(repr=False)
    """(start, stop) of every group, so callers can slice per-group test predictions."""


def cpcv_paths(n_groups: int, k_test: int) -> int:
    """Number of backtest paths phi = (k / N) * C(N, k) (López de Prado 2018, ch. 12, eq. 12.1).

    N = 6, k = 2 -> 5 paths. Each group is tested in C(N-1, k-1) splits, which equals phi.
    """
    if not 1 <= k_test < n_groups:
        raise ValueError("need 1 <= k_test < n_groups")
    return k_test * comb(n_groups, k_test) // n_groups


def cpcv_splits(
    n_obs: int, n_groups: int = 6, k_test: int = 2, purge: int = 0, embargo: int = 0
) -> list[CPCVSplit]:
    """Combinatorial purged cross-validation splits (López de Prado 2018, ch. 7 and 12).

    Observations 0..n_obs-1 are cut into ``n_groups`` contiguous blocks (``np.array_split``).
    For each of the C(N, k) combinations of k test groups: test = those blocks; train = all other
    observations minus
      * purge: the ``purge`` observations immediately before each test block and the ``purge``
        observations immediately after it (training labels that span ``purge`` bars would
        otherwise overlap the test block on either side), and
      * embargo: a further ``embargo`` observations after the purge window following each test
        block (serial-correlation leakage into the training set).
    Combinations are enumerated in lexicographic order; ``path_ids`` follow the assembly rule
    documented in :func:`assemble_cpcv_paths`. Defaults in LEARNING_SYSTEM.md §3.2: N = 6, k = 2,
    purge = label horizon, embargo = 1% of the sample.
    """
    if n_obs < n_groups:
        raise ValueError("n_obs must be >= n_groups")
    if purge < 0 or embargo < 0:
        raise ValueError("purge and embargo must be non-negative")
    n_paths = cpcv_paths(n_groups, k_test)
    groups = np.array_split(np.arange(n_obs, dtype=np.int64), n_groups)
    bounds = tuple((int(g[0]), int(g[-1]) + 1) for g in groups)
    seen = [0] * n_groups  # how many times each group has been tested so far
    out: list[CPCVSplit] = []
    for combo in combinations(range(n_groups), k_test):
        drop = np.zeros(n_obs, dtype=bool)
        test_parts = []
        for g in combo:
            start, stop = bounds[g]
            test_parts.append(groups[g])
            drop[max(0, start - purge) : stop + purge + embargo] = True
        test_idx = np.concatenate(test_parts)
        drop[test_idx] = True
        train_idx = np.flatnonzero(~drop).astype(np.int64)
        path_ids = []
        for g in combo:
            path_ids.append(seen[g])
            seen[g] += 1
        out.append(
            CPCVSplit(
                train_idx=train_idx,
                test_idx=test_idx,
                test_groups=tuple(int(g) for g in combo),
                path_ids=tuple(path_ids),
                group_bounds=bounds,
            )
        )
    assert all(c == n_paths for c in seen)
    return out


def assemble_cpcv_paths(
    splits: Sequence[CPCVSplit], predictions: Sequence[ArrayLike], n_obs: int
) -> FloatArray:
    """Assemble per-split test predictions into full out-of-sample backtest paths.

    ``predictions[i]`` is aligned with ``splits[i].test_idx``. Path-assembly rule (López de
    Prado 2018, ch. 12, fig. 12.1): enumerate the splits in order; the j-th split in which group
    g appears as a test group contributes group g's predictions to path j (``path_ids`` on each
    split record this). Every path then holds exactly one prediction for every observation, i.e.
    one complete OOS sequence, and there are phi = (k/N) C(N, k) such paths.
    Returns an array of shape (n_paths, n_obs) with NaN where nothing was assigned.
    """
    if len(splits) != len(predictions):
        raise ValueError("one prediction array per split")
    n_paths = 1 + max(max(sp.path_ids) for sp in splits)
    paths = np.full((n_paths, n_obs), np.nan)
    for sp, pred in zip(splits, predictions, strict=True):
        p = np.asarray(pred, dtype=np.float64).ravel()
        if p.size != sp.test_idx.size:
            raise ValueError("prediction length must equal the split's test size")
        for g, pid in zip(sp.test_groups, sp.path_ids, strict=True):
            start, stop = sp.group_bounds[g]
            sel = (sp.test_idx >= start) & (sp.test_idx < stop)
            paths[pid, sp.test_idx[sel]] = p[sel]
    return paths


# --------------------------------------------------------------------------------------
# 5. Multiple testing and the Harvey–Liu haircut
# --------------------------------------------------------------------------------------


def multiple_testing_adjust(pvalues: ArrayLike, method: str = "bh") -> FloatArray:
    """Adjusted p-values for a batch of M tests (Harvey & Liu 2015, "Multiple testing framework").

    - ``bonferroni``: p_adj = min(1, M p).
    - ``holm`` (step-down, FWER): sort ascending; p_adj(i) = min(1, max_{j<=i} (M - j + 1) p(j)).
    - ``bh`` (Benjamini & Hochberg 1995, step-up, FDR): p_adj(i) = min(1, min_{j>=i} M p(j) / j).
    Returned in the original order. LEARNING_SYSTEM.md §3.2: BH at FDR 10% across the week's
    batch; Bonferroni for the single candidate that advances.
    """
    p = _as_1d(pvalues, "pvalues")
    if np.any((p < 0) | (p > 1)):
        raise ValueError("p-values must lie in [0, 1]")
    m = p.size
    method = method.lower()
    if method == "bonferroni":
        return np.minimum(1.0, p * m)
    order = np.argsort(p, kind="stable")
    ps = p[order]
    adj_sorted = np.empty(m)
    if method == "holm":
        factors = m - np.arange(m)
        adj_sorted = np.minimum(1.0, np.maximum.accumulate(ps * factors))
    elif method == "bh":
        ranks = np.arange(1, m + 1)
        adj_sorted = np.minimum(1.0, np.minimum.accumulate((ps * m / ranks)[::-1])[::-1])
    else:
        raise ValueError("method must be 'bh', 'bonferroni' or 'holm'")
    out = np.empty(m)
    out[order] = adj_sorted
    return out


@dataclass(frozen=True)
class HaircutResult:
    """Harvey–Liu haircut: single-test p, adjusted p and the Sharpe implied by the adjusted p."""

    sr: float
    p_single: float
    p_adjusted: float
    sr_haircut: float
    haircut_pct: float
    t_stat: float
    method: str
    n_tests: int


def haircut_sharpe(
    sr: float,
    n_obs_months: int,
    n_tests: int,
    method: str = "bh",
    batch_pvalues: ArrayLike | None = None,
) -> HaircutResult:
    """Harvey & Liu (2015) haircut Sharpe ratio.

    ``sr`` is an annualised SR built from ``n_obs_months`` monthly returns. Single-test
    statistic t = (sr / sqrt(12)) * sqrt(T) with two-sided p_S = 2 (1 - Phi(|t|)) (normal
    approximation; the paper uses a t distribution with T - 1 d.o.f., eq. 3). The adjusted
    p_M is then equated to a single-test p-value (eq. 5): sr_haircut = sign(sr) * Z^-1(1 - p_M/2)
    / sqrt(T) * sqrt(12). Paper's example: T = 240, SR 0.75, N = 200 -> p_S = 0.0008,
    p_M ~ 0.15, haircut SR ~ 0.32 (about 60%).

    Adjustment: when only this candidate's p is known (``batch_pvalues`` None) the paper's
    convention is used: the candidate is treated as the most significant of ``n_tests``, so
    Bonferroni gives min(1, N p); Holm's first step-down term is also (N - 1 + 1) p = N p; and BH
    at rank 1 is N p / 1 = N p. **With a single observed p all three methods therefore collapse
    to the Bonferroni bound** — this is a simplification, not the full BH/Holm procedure. Pass
    the week's whole batch of single-test p-values as ``batch_pvalues`` (this candidate's p is
    appended) to run the true step-up / step-down adjustment via
    :func:`multiple_testing_adjust`; ``n_tests`` is then ignored. Gate (LEARNING_SYSTEM.md
    §3.2): haircut SR >= 0.5.
    """
    if n_obs_months < 2:
        raise ValueError("n_obs_months must be >= 2")
    if n_tests < 1:
        raise ValueError("n_tests must be >= 1")
    method = method.lower()
    if method not in {"bh", "bonferroni", "holm"}:
        raise ValueError("method must be 'bh', 'bonferroni' or 'holm'")
    sr_m = sr / math.sqrt(12.0)
    t = sr_m * math.sqrt(n_obs_months)
    p_single = float(2.0 * (1.0 - normal_cdf(abs(t))))
    p_single = min(1.0, max(p_single, 0.0))
    if batch_pvalues is None:
        p_adj = min(1.0, p_single * n_tests)
        n_used = n_tests
    else:
        batch = np.append(_as_1d(batch_pvalues, "batch_pvalues"), p_single)
        p_adj = float(multiple_testing_adjust(batch, method)[-1])
        n_used = batch.size
    if p_adj >= 1.0:
        sr_hc = 0.0
    else:
        z = float(normal_ppf(1.0 - p_adj / 2.0))
        sr_hc = math.copysign(z / math.sqrt(n_obs_months) * math.sqrt(12.0), sr)
    haircut_pct = 0.0 if sr == 0 else (sr - sr_hc) / sr
    return HaircutResult(
        sr=sr,
        p_single=p_single,
        p_adjusted=p_adj,
        sr_haircut=sr_hc,
        haircut_pct=haircut_pct,
        t_stat=t,
        method=method,
        n_tests=n_used,
    )


# --------------------------------------------------------------------------------------
# 6. Random-timing permutation test
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PermResult:
    """Permutation test: ``p_value`` = share of null statistics >= the observed one."""

    p_value: float
    observed: float
    null_dist: FloatArray
    n_perm: int
    statistic: str


def permutation_test_random_timing(
    trade_pnls: ArrayLike,
    bar_returns: ArrayLike,
    holding_bars: int | ArrayLike,
    n_perm: int = 1000,
    rng: np.random.Generator | None = None,
    sides: ArrayLike | None = None,
    statistic: str = "mean",
) -> PermResult:
    """Signal-permutation test against a random-timing null (Masters 2020; research doc §1.8 c).

    Observed statistic = mean (or ``statistic='total'`` sum) of ``trade_pnls``. Null: the same
    number of trades with the same holding periods is placed at uniformly random entry bars on
    ``bar_returns``; each null trade's P&L is side * sum of the bar returns over its holding
    window (long-only unless ``sides`` in {+1, -1} is given, one per trade). ``trade_pnls`` and
    ``bar_returns`` must be in the same units (both fractional returns, or both price points).
    p = share of null statistics >= observed (no +1 smoothing). Gate (LEARNING_SYSTEM.md §3.2):
    p < 0.05.
    """
    gen = _check_rng(rng)
    pnl = _as_1d(trade_pnls, "trade_pnls")
    bars = _as_1d(bar_returns, "bar_returns")
    n_tr = pnl.size
    hold = np.broadcast_to(np.asarray(holding_bars, dtype=np.int64), (n_tr,)).copy()
    if np.any(hold < 1):
        raise ValueError("holding_bars must be >= 1")
    if np.any(hold > bars.size):
        raise ValueError("a holding period exceeds the bar series length")
    if sides is None:
        side = np.ones(n_tr)
    else:
        side = _as_1d(sides, "sides")
        if side.size != n_tr or not np.all(np.isin(side, (-1.0, 1.0))):
            raise ValueError("sides must be +1/-1, one per trade")
    if statistic not in {"mean", "total"}:
        raise ValueError("statistic must be 'mean' or 'total'")
    agg = np.mean if statistic == "mean" else np.sum
    observed = float(agg(pnl))

    cs = np.concatenate(([0.0], np.cumsum(bars)))
    max_start = bars.size - hold  # inclusive upper bound per trade
    u = gen.random((n_perm, n_tr))
    entries = np.floor(u * (max_start + 1)).astype(np.int64)
    null_pnl = side * (cs[entries + hold] - cs[entries])
    null = np.asarray(agg(null_pnl, axis=1), dtype=np.float64)
    p = float(np.mean(null >= observed))
    return PermResult(
        p_value=p, observed=observed, null_dist=null, n_perm=n_perm, statistic=statistic
    )


# --------------------------------------------------------------------------------------
# 7. Drawdown and trade-order Monte Carlo
# --------------------------------------------------------------------------------------


def max_drawdown(equity_or_pnl: ArrayLike, cumulative: bool = False) -> float:
    """Maximum peak-to-trough drawdown, as a positive number in P&L units.

    With ``cumulative=False`` the input is a sequence of P&L increments (per trade or per bar)
    and the equity curve is its cumulative sum starting from 0; with ``cumulative=True`` the
    input is already an equity curve. The running peak starts at the first equity value (0 for
    increments), so an initial losing streak counts as drawdown.
    """
    x = _as_1d(equity_or_pnl, "equity_or_pnl")
    eq = x if cumulative else np.concatenate(([0.0], np.cumsum(x)))
    peak = np.maximum.accumulate(eq)
    return float(np.max(peak - eq))


@dataclass(frozen=True)
class MCDDResult:
    """Trade-order Monte Carlo drawdown distribution and where the observed drawdown sits."""

    dd_p50: float
    dd_p95: float
    dd_p99: float
    observed_dd: float
    observed_percentile: float
    null_dist: FloatArray
    n_perm: int


def monte_carlo_drawdown(
    trade_pnls: ArrayLike, n_perm: int = 10000, rng: np.random.Generator | None = None
) -> MCDDResult:
    """Max-drawdown distribution from ``n_perm`` random shufflings of the trade order (Masters
    2020; research doc §1.8 a). ``observed_percentile`` is the share of shuffled drawdowns that
    are <= the observed one. Monitor (LEARNING_SYSTEM.md §4): live DD > 95th percentile -> Pause,
    > 99th -> Retire. Permutation keeps the trade distribution but destroys any autocorrelation.
    """
    gen = _check_rng(rng)
    pnl = _as_1d(trade_pnls, "trade_pnls")
    observed = max_drawdown(pnl)
    perm = gen.permuted(np.broadcast_to(pnl, (n_perm, pnl.size)).copy(), axis=1)
    eq = np.cumsum(perm, axis=1)
    peak = np.maximum.accumulate(np.maximum(eq, 0.0), axis=1)
    dd = np.max(peak - eq, axis=1)
    p50, p95, p99 = np.percentile(dd, [50, 95, 99])
    return MCDDResult(
        dd_p50=float(p50),
        dd_p95=float(p95),
        dd_p99=float(p99),
        observed_dd=observed,
        observed_percentile=float(np.mean(dd <= observed)),
        null_dist=dd,
        n_perm=n_perm,
    )


# --------------------------------------------------------------------------------------
# 8. Bootstrap confidence intervals
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapCI:
    """Percentile bootstrap interval: ``point`` is the statistic on the original sample."""

    point: float
    lower: float
    upper: float
    alpha: float
    n_boot: int
    method: str


def _apply_stat(stat: Callable[..., ArrayLike], samples: FloatArray) -> FloatArray:
    """Evaluate ``stat`` on each row of ``samples``; vectorised when ``stat`` accepts ``axis``."""
    try:
        out = np.asarray(stat(samples, axis=1), dtype=np.float64)
        if out.shape == (samples.shape[0],):
            return out
    except TypeError:
        pass
    return np.asarray([float(np.asarray(stat(row), dtype=np.float64)) for row in samples])


def bootstrap_ci(
    values: ArrayLike,
    stat: Callable[..., ArrayLike] = np.mean,
    n_boot: int = 5000,
    alpha: float = 0.05,
    rng: np.random.Generator | None = None,
) -> BootstrapCI:
    """IID percentile bootstrap (1 - alpha) CI of ``stat`` (Efron). Demo gate
    (LEARNING_SYSTEM.md §3.3): lower bound of expectancy after costs > 0.
    """
    gen = _check_rng(rng)
    v = _as_1d(values, "values")
    idx = gen.integers(0, v.size, size=(n_boot, v.size))
    stats = _apply_stat(stat, v[idx])
    lo, hi = np.percentile(stats, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return BootstrapCI(
        float(np.asarray(stat(v), dtype=np.float64)), float(lo), float(hi), alpha, n_boot, "iid"
    )


def block_bootstrap_ci(
    returns: ArrayLike,
    block_len: int,
    stat: Callable[..., ArrayLike] = np.mean,
    n_boot: int = 5000,
    alpha: float = 0.05,
    rng: np.random.Generator | None = None,
    method: str = "stationary",
) -> BootstrapCI:
    """Block bootstrap CI for serially dependent returns.

    ``method='stationary'`` (Politis & Romano 1994): blocks start at random positions and have
    geometric lengths with mean ``block_len`` (continuation probability 1 - 1/block_len), wrapping
    circularly. ``method='circular'`` (Politis & Romano 1992): fixed-length blocks with random
    starts, wrapping circularly. Each resample has the original length. A block length of about
    sqrt(T) is the usual default (research doc §1.7).
    """
    gen = _check_rng(rng)
    r = _as_1d(returns, "returns")
    n = r.size
    if block_len < 1:
        raise ValueError("block_len must be >= 1")
    if method == "circular":
        n_blocks = -(-n // block_len)
        starts = gen.integers(0, n, size=(n_boot, n_blocks))
        idx = (starts[:, :, None] + np.arange(block_len)) % n
        idx = idx.reshape(n_boot, -1)[:, :n]
    elif method == "stationary":
        p_new = 1.0 / block_len
        idx = np.empty((n_boot, n), dtype=np.int64)
        idx[:, 0] = gen.integers(0, n, size=n_boot)
        for t in range(1, n):
            new_block = gen.random(n_boot) < p_new
            cont = (idx[:, t - 1] + 1) % n
            fresh = gen.integers(0, n, size=n_boot)
            idx[:, t] = np.where(new_block, fresh, cont)
    else:
        raise ValueError("method must be 'stationary' or 'circular'")
    stats = _apply_stat(stat, r[idx])
    lo, hi = np.percentile(stats, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return BootstrapCI(
        float(np.asarray(stat(r), dtype=np.float64)), float(lo), float(hi), alpha, n_boot, method
    )


# --------------------------------------------------------------------------------------
# 9. Production monitors (LEARNING_SYSTEM.md §4)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CusumResult:
    """Lower-side CUSUM path; ``breached_at`` is the first index with S_t > h (None if never)."""

    statistic_path: FloatArray
    breached_at: int | None
    h: float
    k: float
    mu0: float

    @property
    def breached(self) -> bool:
        return self.breached_at is not None


def cusum(returns: ArrayLike, mu0: float, k: float, h: float) -> CusumResult:
    """One-sided (lower) CUSUM for a downward shift in the mean (Page 1954).

    S_0 = 0, S_t = max(0, S_{t-1} + (mu0 - k - r_t)); breach when S_t > h. ``mu0`` is the
    reference mean (backtest expectancy per trade), ``k`` the allowance (half the shift to
    detect) and ``h`` the decision interval. The research doc §3.1 writes the recursion for an
    upward shift; this is its mirror image for detecting decay. Defaults (LEARNING_SYSTEM.md
    §4, per-trade R): k = 0.5 sigma, h = 4 sigma from the offline distribution -> Pause.
    """
    r = _as_1d(returns, "returns")
    if h <= 0 or k < 0:
        raise ValueError("h must be positive and k non-negative")
    path = np.empty(r.size)
    s = 0.0
    breached_at: int | None = None
    for i, x in enumerate(r):
        s = max(0.0, s + (mu0 - k - x))
        path[i] = s
        if breached_at is None and s > h:
            breached_at = i
    return CusumResult(path, breached_at, h, k, mu0)


@dataclass(frozen=True)
class RollingSharpeResult:
    """Rolling annualised SR with Lo (2002) bands around a reference SR.

    ``rolling_sr[i]`` covers returns (i - window + 1 .. i) and is NaN before the first full
    window. ``below_lower`` flags windows ending below the reference's lower band;
    ``trailing_below`` counts how many consecutive trailing observations are flagged; ``pause``
    is True when that run spans at least ``2 * window`` observations (two full windows below the
    band, LEARNING_SYSTEM.md §4).
    """

    rolling_sr: FloatArray
    lower: float
    upper: float
    sr_ref: float
    window: int
    below_lower: NDArray[np.bool_]
    trailing_below: int
    pause: bool


def rolling_sharpe_bands(
    returns: ArrayLike,
    window: int,
    sr_ref: float,
    periods_per_year: float = 1.0,
    z: float = 1.96,
) -> RollingSharpeResult:
    """Rolling SR over the last ``window`` observations against Lo (2002) bands of a reference.

    Band = sr_ref +/- z * SE with SE = sqrt((1 + 0.5 sr_ref_p^2) / window) at the observation
    frequency (sr_ref_p = sr_ref / sqrt(periods_per_year)), scaled back by sqrt(periods_per_year).
    Default monitor (LEARNING_SYSTEM.md §4): window 100 trades; live below the backtest lower
    band for 2 windows -> Pause. The research doc notes the band is useless below ~60 trades.
    """
    r = _as_1d(returns, "returns")
    if window < 2:
        raise ValueError("window must be >= 2")
    scale = math.sqrt(periods_per_year)
    se = sharpe_se(sr_ref / scale, window) * scale
    lower, upper = sr_ref - z * se, sr_ref + z * se
    n = r.size
    roll = np.full(n, np.nan)
    if n >= window:
        cs = np.concatenate(([0.0], np.cumsum(r)))
        cs2 = np.concatenate(([0.0], np.cumsum(r * r)))
        wsum = cs[window:] - cs[:-window]
        wsq = cs2[window:] - cs2[:-window]
        mu = wsum / window
        var = np.maximum((wsq - window * mu * mu) / (window - 1), 0.0)
        sd = np.sqrt(var)
        with np.errstate(divide="ignore", invalid="ignore"):
            roll[window - 1 :] = np.where(sd > 0, mu / sd * scale, 0.0)
    below = np.zeros(n, dtype=bool)
    valid = ~np.isnan(roll)
    below[valid] = roll[valid] < lower
    trailing = 0
    for flag in below[::-1]:
        if not flag:
            break
        trailing += 1
    return RollingSharpeResult(
        rolling_sr=roll,
        lower=lower,
        upper=upper,
        sr_ref=sr_ref,
        window=window,
        below_lower=below,
        trailing_below=trailing,
        pause=trailing >= 2 * window,
    )


@dataclass(frozen=True)
class PChartResult:
    """Binomial p-chart around a reference hit rate; ``breached`` needs n >= min_n and p_hat < lcl."""

    p_hat: float
    lcl: float
    ucl: float
    p0: float
    n: int
    breached: bool


def hit_rate_pchart(
    wins: int, n: int, p0: float, sigma: float = 3.0, min_n: int = 50
) -> PChartResult:
    """Hit-rate p-chart: control limits p0 +/- sigma * sqrt(p0 (1 - p0) / n), clipped to [0, 1].

    Default (LEARNING_SYSTEM.md §4): breach of the lower 3-sigma limit on n >= 50 trades -> Warn.
    Low power, cheap early warning (research doc §3.1).
    """
    if n < 1 or wins < 0 or wins > n:
        raise ValueError("need 0 <= wins <= n and n >= 1")
    if not 0.0 < p0 < 1.0:
        raise ValueError("p0 must lie strictly inside (0, 1)")
    half = sigma * math.sqrt(p0 * (1.0 - p0) / n)
    lcl = max(0.0, p0 - half)
    ucl = min(1.0, p0 + half)
    p_hat = wins / n
    return PChartResult(p_hat, lcl, ucl, p0, n, bool(n >= min_n and p_hat < lcl))
