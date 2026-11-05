"""Tests for goldtrader.backtest.stats (offline-gate statistics and production monitors)."""

from __future__ import annotations

import math
from math import comb

import numpy as np
import pytest

from goldtrader.backtest import stats as st

# --------------------------------------------------------------------------------------
# normal helpers
# --------------------------------------------------------------------------------------


def test_normal_cdf_and_ppf_known_values():
    assert st.normal_cdf(0.0) == pytest.approx(0.5)
    assert st.normal_cdf(1.959963985) == pytest.approx(0.975, abs=1e-9)
    assert st.normal_ppf(0.975) == pytest.approx(1.959963985, abs=1e-9)
    assert st.normal_ppf(0.5) == pytest.approx(0.0, abs=1e-12)
    assert st.normal_ppf(0.95) == pytest.approx(1.644853627, abs=1e-9)


def test_normal_ppf_inverts_cdf_in_tails_and_vectorised():
    ps = np.array([1e-12, 1e-6, 0.01, 0.3, 0.7, 0.99, 1 - 1e-9])
    back = st.normal_cdf(st.normal_ppf(ps))
    assert np.allclose(back, ps, rtol=1e-9, atol=1e-15)
    assert st.normal_ppf(0.0) == -math.inf and st.normal_ppf(1.0) == math.inf


# --------------------------------------------------------------------------------------
# 1. Sharpe, Lo SE, minimum trades
# --------------------------------------------------------------------------------------


def test_sharpe_basic_and_degenerate():
    r = np.array([0.01, 0.02, -0.01, 0.03])
    expected = r.mean() / r.std(ddof=1) * math.sqrt(252)
    assert st.sharpe(r, 252) == pytest.approx(expected)
    assert st.sharpe(np.array([0.01, 0.01, 0.01])) == 0.0  # zero variance
    assert st.sharpe(np.array([0.01])) == 0.0


def test_sharpe_se_matches_lo_eq9():
    # SE = sqrt((1 + 0.5 SR^2) / n)
    assert st.sharpe_se(0.0, 100) == pytest.approx(0.1)
    assert st.sharpe_se(1.5, 60) == pytest.approx(math.sqrt((1 + 0.5 * 2.25) / 60))
    with pytest.raises(ValueError):
        st.sharpe_se(1.0, 0)


def test_sharpe_ci_is_symmetric_and_scales_with_frequency():
    rng = np.random.default_rng(1)
    r = rng.standard_normal(400) * 0.01 + 0.001
    ci = st.sharpe_ci(r, periods_per_year=252, alpha=0.05)
    assert ci.lower < ci.sr < ci.upper
    assert ci.upper - ci.sr == pytest.approx(ci.sr - ci.lower)
    per_period = st.sharpe(r, 1.0)
    assert ci.se == pytest.approx(st.sharpe_se(per_period, 400) * math.sqrt(252))
    assert ci.n == 400


def test_min_trades_matches_doc_examples():
    # docs/research/04 §7: annual SR 1.0, 250 trades/yr -> ~680; 1.5 -> ~300; 2.0 -> ~170
    assert 670 <= st.min_trades_for_sharpe(1.0, 250) <= 690
    assert 290 <= st.min_trades_for_sharpe(1.5, 250) <= 310
    assert 165 <= st.min_trades_for_sharpe(2.0, 250) <= 180
    # two-sided needs more trades than one-sided
    assert st.min_trades_for_sharpe(1.0, 250, one_sided=False) > st.min_trades_for_sharpe(1.0, 250)


# --------------------------------------------------------------------------------------
# 2. E[max SR], DSR, MinBTL
# --------------------------------------------------------------------------------------


def test_expected_max_sharpe_paper_value_and_bound():
    # Charlatanism paper: N = 10 -> E[max] = 1.57
    assert st.expected_max_sharpe(10) == pytest.approx(1.57, abs=0.01)
    prev = 0.0
    for n in (2, 5, 10, 45, 100, 1000, 10_000):
        e = st.expected_max_sharpe(n)
        assert e > prev
        assert e <= math.sqrt(2 * math.log(n))  # paper's upper bound
        prev = e
    assert st.expected_max_sharpe(1000) > 0.8 * math.sqrt(2 * math.log(1000))
    assert st.expected_max_sharpe(1) == 0.0
    # variance scaling
    assert st.expected_max_sharpe(50, 4.0) == pytest.approx(2 * st.expected_max_sharpe(50, 1.0))


def test_min_backtest_length_paper_example():
    # Theorem 3.1: 5 years of data -> at most ~45 independent trials at E[max] = 1
    assert st.min_backtest_length_years(45) == pytest.approx(5.0, abs=0.1)
    assert 43 <= st.max_trials_for_backtest_length(5.0) <= 47
    # exact form is below the 2 ln N / E^2 upper bound, which falls with a higher target
    assert st.min_backtest_length_years(45) < st.min_backtest_length_years(45, upper_bound=True)
    assert st.min_backtest_length_years(45, upper_bound=True) == pytest.approx(2 * math.log(45))
    assert st.min_backtest_length_years(45, target_max_sr=2.0) == pytest.approx(5.0 / 4, abs=0.05)
    assert st.min_backtest_length_years(1) == 0.0


def test_skew_kurt_of_normal_sample():
    rng = np.random.default_rng(3)
    sk, ku = st.skew_kurt(rng.standard_normal(200_000))
    assert abs(sk) < 0.03
    assert ku == pytest.approx(3.0, abs=0.05)
    assert st.skew_kurt(np.array([1.0, 1.0, 1.0])) == (0.0, 3.0)


def test_deflated_sharpe_near_one_for_huge_true_edge():
    rng = np.random.default_rng(4)
    r = rng.standard_normal(1000) + 0.5  # per-period SR ~ 0.5
    sk, ku = st.skew_kurt(r)
    dsr = st.deflated_sharpe(
        st.sharpe(r), n_trials=100, var_sr=1 / 1000, n_obs=1000, skew=sk, kurt=ku
    )
    assert dsr > 0.999


def test_deflated_sharpe_around_half_when_best_of_noise_is_selected():
    vals = []
    for seed in range(20):
        rng = np.random.default_rng(seed)
        t, n = 500, 100
        x = rng.standard_normal((t, n))
        srs = x.mean(axis=0) / x.std(axis=0, ddof=1)
        best = int(np.argmax(srs))
        sk, ku = st.skew_kurt(x[:, best])
        vals.append(st.deflated_sharpe(srs[best], n, srs.var(ddof=1), t, sk, ku))
    mean = float(np.mean(vals))
    assert 0.3 < mean < 0.7
    assert max(vals) < 0.95  # none of the lucky picks clears the gate


def test_deflated_sharpe_equals_psr_with_expected_max_benchmark():
    sr_star = st.expected_max_sharpe(30, 0.01)
    assert st.deflated_sharpe(0.3, 30, 0.01, 250, 0.1, 4.0) == pytest.approx(
        st.probabilistic_sharpe(0.3, sr_star, 250, 0.1, 4.0)
    )
    # higher N deflates more
    assert st.deflated_sharpe(0.2, 5, 0.01, 250) > st.deflated_sharpe(0.2, 500, 0.01, 250)


# --------------------------------------------------------------------------------------
# 3. PBO / CSCV
# --------------------------------------------------------------------------------------


def test_pbo_small_when_one_trial_has_true_edge():
    rng = np.random.default_rng(10)
    m = rng.standard_normal((2000, 20))
    m[:, 7] += 0.15
    res = st.pbo_cscv(m, s=16)
    assert res.n_combinations == comb(16, 8) == 12870
    assert res.pbo < 0.15
    assert res.n_trials == 20 and res.n_rows_used == 2000 and not res.sampled
    assert np.mean(res.selected == 7) > 0.9
    assert res.logits.shape == (12870,)
    assert res.is_metric.shape == res.oos_metric.shape == (12870,)


def test_pbo_large_when_all_trials_are_noise():
    pbos = [
        st.pbo_cscv(np.random.default_rng(s).standard_normal((2000, 20)), s=10).pbo
        for s in range(10)
    ]
    assert float(np.mean(pbos)) > 0.4
    res = st.pbo_cscv(np.random.default_rng(5).standard_normal((2000, 20)), s=10)
    assert res.n_combinations == comb(10, 5) == 252
    assert 0.0 <= res.prob_oos_loss <= 1.0


def test_pbo_generic_metric_matches_default_and_sampling_guard():
    rng = np.random.default_rng(11)
    m = rng.standard_normal((600, 8))
    m[:, 2] += 0.1
    a = st.pbo_cscv(m, s=6)
    b = st.pbo_cscv(m, s=6, metric=lambda x: x.mean(axis=0) / x.std(axis=0, ddof=1))
    assert np.allclose(a.logits, b.logits)
    assert a.pbo == b.pbo
    # combinations exceeding the cap are sampled with the rng
    c = st.pbo_cscv(m, s=10, max_combinations=50, rng=np.random.default_rng(0))
    assert c.sampled and c.n_combinations == 50
    with pytest.raises(ValueError):
        st.pbo_cscv(m, s=10, max_combinations=50)  # rng required when sampling
    # leftover rows (oldest) are dropped so blocks are equal
    d = st.pbo_cscv(m[:605], s=6)
    assert d.n_rows_used == 600
    with pytest.raises(ValueError):
        st.pbo_cscv(m, s=7)


def test_pbo_degradation_regression_is_negative_for_noise():
    rng = np.random.default_rng(12)
    res = st.pbo_cscv(rng.standard_normal((1200, 30)), s=8)
    # IS-best of pure noise: OOS performance regresses towards zero -> negative slope typical
    assert res.oos_degradation_slope < 0.5
    assert np.isfinite(res.oos_degradation_intercept)


# --------------------------------------------------------------------------------------
# 4. CPCV
# --------------------------------------------------------------------------------------


def test_cpcv_counts_n6_k2():
    assert st.cpcv_paths(6, 2) == 5
    assert st.cpcv_paths(10, 2) == 9
    splits = st.cpcv_splits(600, 6, 2)
    assert len(splits) == comb(6, 2) == 15
    # every observation is a test observation in exactly C(N-1, k-1) = 5 splits
    counts = np.zeros(600, dtype=int)
    for sp in splits:
        counts[sp.test_idx] += 1
        assert np.intersect1d(sp.train_idx, sp.test_idx).size == 0
        assert sp.train_idx.size + sp.test_idx.size == 600  # no purge/embargo
    assert np.all(counts == comb(5, 1))
    # path ids: each path id appears exactly once per group across splits
    for g in range(6):
        pids = sorted(
            pid for sp in splits for tg, pid in zip(sp.test_groups, sp.path_ids) if tg == g
        )
        assert pids == [0, 1, 2, 3, 4]


def test_cpcv_purge_and_embargo_remove_adjacent_indices():
    splits = st.cpcv_splits(60, 6, 2, purge=2, embargo=3)
    by_groups = {sp.test_groups: sp for sp in splits}
    sp = by_groups[(2, 4)]  # test blocks 20..29 and 40..49
    assert list(sp.test_idx) == list(range(20, 30)) + list(range(40, 50))
    expected_train = list(range(18)) + [35, 36, 37] + list(range(55, 60))
    assert list(sp.train_idx) == expected_train
    sp01 = by_groups[(0, 1)]  # contiguous test blocks at the start
    assert list(sp01.train_idx) == list(range(25, 60))
    sp45 = by_groups[(4, 5)]  # test at the end: only the leading purge applies
    assert list(sp45.train_idx) == list(range(38))


def test_cpcv_path_assembly_gives_complete_oos_sequences():
    n = 60
    splits = st.cpcv_splits(n, 6, 2)
    preds = [sp.test_idx.astype(float) for sp in splits]  # identity predictions
    paths = st.assemble_cpcv_paths(splits, preds, n)
    assert paths.shape == (5, n)
    assert not np.isnan(paths).any()
    assert np.allclose(paths, np.arange(n)[None, :])
    # predictions tagged with the split index: each path uses one distinct split per group
    tagged = [np.full(sp.test_idx.size, i, dtype=float) for i, sp in enumerate(splits)]
    paths2 = st.assemble_cpcv_paths(splits, tagged, n)
    for g in range(6):
        start, stop = splits[0].group_bounds[g]
        per_path = paths2[:, start:stop]
        assert np.all(per_path == per_path[:, :1])  # constant inside a group
        assert len(set(per_path[:, 0].tolist())) == 5  # five different splits across paths
    with pytest.raises(ValueError):
        st.cpcv_splits(5, 6, 2)
    with pytest.raises(ValueError):
        st.cpcv_paths(6, 6)


# --------------------------------------------------------------------------------------
# 5. Multiple testing, haircut
# --------------------------------------------------------------------------------------


def test_bonferroni_holm_bh_small_cases():
    p = np.array([0.01, 0.04, 0.03, 0.20])
    bonf = st.multiple_testing_adjust(p, "bonferroni")
    assert np.allclose(bonf, [0.04, 0.16, 0.12, 0.80])
    holm = st.multiple_testing_adjust(p, "holm")
    # sorted: 0.01*4=0.04, 0.03*3=0.09, 0.04*2=0.08->max(0.09)=0.09, 0.20*1->max=0.20
    assert np.allclose(holm, [0.04, 0.09, 0.09, 0.20])
    bh = st.multiple_testing_adjust(p, "bh")
    # sorted: 0.01*4/1=0.04, 0.03*4/2=0.06, 0.04*4/3=0.0533, 0.20*4/4=0.20 -> step-up min
    assert np.allclose(bh, [0.04, 0.0533333, 0.0533333, 0.20])
    assert np.all(st.multiple_testing_adjust([0.5, 0.9], "bonferroni") == [1.0, 1.0])
    with pytest.raises(ValueError):
        st.multiple_testing_adjust(p, "fdr-magic")
    with pytest.raises(ValueError):
        st.multiple_testing_adjust([1.2], "bh")


def test_haircut_sharpe_reproduces_harvey_liu_example():
    # Harvey & Liu 2015: T = 240 months, SR 0.75, N = 200 -> p_S 0.0008, p_M 0.15, HSR 0.32
    res = st.haircut_sharpe(0.75, 240, 200)
    assert res.p_single == pytest.approx(0.0008, abs=0.0001)
    assert 0.12 <= res.p_adjusted <= 0.18
    assert 0.25 <= res.sr_haircut <= 0.40
    assert 0.45 <= res.haircut_pct <= 0.70
    # single p: all three methods collapse to the Bonferroni bound (documented simplification)
    for m in ("bh", "holm", "bonferroni"):
        assert st.haircut_sharpe(0.75, 240, 200, method=m).sr_haircut == pytest.approx(
            res.sr_haircut
        )


def test_haircut_is_nonlinear_and_handles_batches():
    # marginal SRs are hit hardest, high SRs only moderately (Harvey & Liu)
    low = st.haircut_sharpe(0.5, 120, 100)
    high = st.haircut_sharpe(3.0, 120, 100)
    assert low.haircut_pct > high.haircut_pct
    assert low.sr_haircut == 0.0  # adjusted p >= 1 -> no evidence left
    # with one test there is no haircut
    one = st.haircut_sharpe(1.0, 120, 1)
    assert one.sr_haircut == pytest.approx(1.0, abs=1e-9)
    # negative SR keeps its sign
    assert st.haircut_sharpe(-2.0, 240, 2).sr_haircut < 0
    # full batch: a candidate accompanied by many tiny p-values gets a milder BH adjustment
    batch = np.full(199, 1e-6)
    bh_batch = st.haircut_sharpe(0.75, 240, 200, method="bh", batch_pvalues=batch)
    assert bh_batch.p_adjusted < st.haircut_sharpe(0.75, 240, 200).p_adjusted
    assert bh_batch.n_tests == 200


# --------------------------------------------------------------------------------------
# 6. Random-timing permutation test
# --------------------------------------------------------------------------------------


def _random_timing_trades(rng, bars, n_trades=30):
    hold = rng.integers(2, 10, n_trades)
    entries = rng.integers(0, bars.size - 10, n_trades)
    cs = np.concatenate(([0.0], np.cumsum(bars)))
    return cs[entries + hold] - cs[entries], hold


def test_permutation_p_roughly_uniform_under_null():
    ps = []
    for seed in range(50):
        rng = np.random.default_rng(seed)
        bars = rng.standard_normal(2000) * 0.01
        pnl, hold = _random_timing_trades(rng, bars)
        ps.append(st.permutation_test_random_timing(pnl, bars, hold, n_perm=200, rng=rng).p_value)
    assert abs(float(np.mean(ps)) - 0.5) < 0.15
    assert min(ps) < 0.25 and max(ps) > 0.75


def test_permutation_p_small_for_strong_edge_and_sides():
    rng = np.random.default_rng(99)
    bars = rng.standard_normal(2000) * 0.01
    pnl, hold = _random_timing_trades(rng, bars)
    pnl = pnl + 0.03  # strong edge relative to ~0.01 * sqrt(hold) noise
    res = st.permutation_test_random_timing(pnl, bars, hold, n_perm=500, rng=rng)
    assert res.p_value < 0.01
    assert res.null_dist.shape == (500,) and res.observed == pytest.approx(pnl.mean())
    total = st.permutation_test_random_timing(pnl, bars, hold, 100, rng, statistic="total")
    assert total.observed == pytest.approx(pnl.sum())
    # shorts on an up-drifting series lose under the null -> a flat strategy looks significant
    up = np.full(500, 0.001)
    sides = -np.ones(10)
    res_short = st.permutation_test_random_timing(np.zeros(10), up, 5, 200, rng, sides=sides)
    assert res_short.p_value == 0.0
    with pytest.raises(ValueError):
        st.permutation_test_random_timing(pnl, bars, hold, 10, rng, sides=np.zeros(pnl.size))
    with pytest.raises(ValueError):
        st.permutation_test_random_timing(pnl, bars, 5000, 10, rng)


# --------------------------------------------------------------------------------------
# 7. Drawdown Monte Carlo
# --------------------------------------------------------------------------------------


def test_max_drawdown_known_values():
    assert st.max_drawdown([1, -2, 1, 1, -3]) == pytest.approx(3.0)  # peak 1 -> trough -2 ...
    assert st.max_drawdown([100, 110, 95, 120, 90], cumulative=True) == pytest.approx(30.0)
    assert st.max_drawdown([1, 2, 3]) == 0.0
    assert st.max_drawdown([-1, -1]) == pytest.approx(2.0)  # initial losing streak counts


def test_monte_carlo_drawdown_percentiles_ordered():
    rng = np.random.default_rng(7)
    pnl = rng.standard_normal(200) + 0.1
    res = st.monte_carlo_drawdown(pnl, n_perm=2000, rng=rng)
    assert res.dd_p50 <= res.dd_p95 <= res.dd_p99
    assert res.dd_p50 > 0
    assert res.observed_dd == pytest.approx(st.max_drawdown(pnl))
    assert 0.0 <= res.observed_percentile <= 1.0
    assert res.null_dist.shape == (2000,)
    # a deliberately clustered losing streak sits above the 95th percentile
    sorted_pnl = np.sort(pnl)  # all losses first
    bad = st.monte_carlo_drawdown(sorted_pnl, n_perm=2000, rng=rng)
    assert bad.observed_dd > bad.dd_p99
    assert bad.observed_percentile > 0.99


# --------------------------------------------------------------------------------------
# 8. Bootstrap
# --------------------------------------------------------------------------------------


def test_bootstrap_ci_covers_mean_and_is_deterministic():
    rng = np.random.default_rng(21)
    x = rng.standard_normal(300) * 2 + 1.0
    ci = st.bootstrap_ci(x, n_boot=3000, rng=np.random.default_rng(0))
    assert ci.lower < ci.point < ci.upper
    assert ci.point == pytest.approx(x.mean())
    # percentile interval of the mean ~ analytic normal interval x_bar +/- 1.96 s/sqrt(n)
    half = 1.96 * x.std(ddof=1) / math.sqrt(x.size)
    assert ci.lower == pytest.approx(x.mean() - half, abs=0.15 * half)
    assert ci.upper == pytest.approx(x.mean() + half, abs=0.15 * half)
    # coverage of the true mean over many samples is about 95%
    covered = 0
    for seed in range(60):
        g = np.random.default_rng(seed)
        y = g.standard_normal(80) + 1.0
        c = st.bootstrap_ci(y, n_boot=400, rng=g)
        covered += c.lower < 1.0 < c.upper
    assert covered >= 50
    again = st.bootstrap_ci(x, n_boot=3000, rng=np.random.default_rng(0))
    assert (ci.lower, ci.upper) == (again.lower, again.upper)
    # non-vectorised statistic falls back to a loop
    ci_sr = st.bootstrap_ci(
        x, stat=lambda v: st.sharpe(v), n_boot=300, rng=np.random.default_rng(1)
    )
    assert ci_sr.lower < ci_sr.point < ci_sr.upper
    with pytest.raises(ValueError):
        st.bootstrap_ci(x)


def test_block_bootstrap_ci_stationary_and_circular():
    rng = np.random.default_rng(22)
    # AR(1) series with positive autocorrelation and mean 0.2
    e = rng.standard_normal(600)
    r = np.empty(600)
    r[0] = e[0]
    for t in range(1, 600):
        r[t] = 0.6 * r[t - 1] + e[t]
    r += 0.2
    iid = st.bootstrap_ci(r, n_boot=2000, rng=np.random.default_rng(0))
    stat = st.block_bootstrap_ci(
        r, 25, n_boot=2000, rng=np.random.default_rng(0), method="stationary"
    )
    circ = st.block_bootstrap_ci(
        r, 25, n_boot=2000, rng=np.random.default_rng(0), method="circular"
    )
    for ci in (stat, circ):
        assert ci.lower < ci.point < ci.upper
        # positive autocorrelation -> block intervals wider than the IID one
        assert (ci.upper - ci.lower) > (iid.upper - iid.lower)
    assert stat.method == "stationary" and circ.method == "circular"
    with pytest.raises(ValueError):
        st.block_bootstrap_ci(r, 25, rng=rng, method="moving")


# --------------------------------------------------------------------------------------
# 9. Monitors
# --------------------------------------------------------------------------------------


def test_cusum_breaches_on_regime_shift_not_on_stationary_data():
    rng = np.random.default_rng(31)
    sigma, mu0 = 1.0, 0.3
    k, h = 0.5 * sigma, 4.0 * sigma
    stationary = rng.standard_normal(400) * sigma + mu0
    res = st.cusum(stationary, mu0, k, h)
    assert res.breached_at is None and not res.breached
    assert res.statistic_path.shape == (400,) and np.all(res.statistic_path >= 0)
    shifted = np.concatenate([stationary[:200], rng.standard_normal(200) * sigma + (mu0 - 1.5)])
    res2 = st.cusum(shifted, mu0, k, h)
    assert res2.breached and res2.breached_at is not None
    assert 200 <= res2.breached_at < 240
    assert res2.statistic_path[res2.breached_at] > h
    with pytest.raises(ValueError):
        st.cusum(shifted, mu0, k, 0.0)


def test_cusum_recursion_by_hand():
    res = st.cusum([0.0, -1.0, 2.0, -0.5], mu0=0.0, k=0.25, h=10.0)
    # S1 = max(0, 0 - 0.25 - 0) = 0; S2 = 0 - 0.25 + 1 = 0.75; S3 = max(0, 0.75 - 0.25 - 2) = 0
    # S4 = 0 - 0.25 + 0.5 = 0.25
    assert np.allclose(res.statistic_path, [0.0, 0.75, 0.0, 0.25])


def test_rolling_sharpe_bands_flag_decay():
    rng = np.random.default_rng(41)
    window = 50
    good = rng.standard_normal(200) * 0.01 + 0.004  # per-trade SR ~ 0.4
    sr_ref = st.sharpe(good, 1.0)
    res = st.rolling_sharpe_bands(good, window, sr_ref)
    assert (
        np.isnan(res.rolling_sr[: window - 1]).all()
        and not np.isnan(res.rolling_sr[window - 1 :]).any()
    )
    assert res.lower < sr_ref < res.upper
    assert res.upper - sr_ref == pytest.approx(1.96 * st.sharpe_se(sr_ref, window))
    assert not res.pause
    decayed = np.concatenate([good, rng.standard_normal(150) * 0.01 - 0.004])
    res2 = st.rolling_sharpe_bands(decayed, window, sr_ref)
    assert res2.below_lower[-1]
    assert res2.trailing_below >= 2 * window and res2.pause
    # rolling value equals the plain Sharpe of the window
    assert res2.rolling_sr[-1] == pytest.approx(st.sharpe(decayed[-window:], 1.0))
    # annualisation scales both the estimate and the band
    ann = st.rolling_sharpe_bands(good, window, sr_ref * math.sqrt(252), periods_per_year=252)
    assert ann.rolling_sr[-1] == pytest.approx(res.rolling_sr[-1] * math.sqrt(252))
    assert ann.lower == pytest.approx(res.lower * math.sqrt(252))


def test_hit_rate_pchart_limits_and_breach():
    res = st.hit_rate_pchart(wins=20, n=100, p0=0.5)
    assert res.lcl == pytest.approx(0.5 - 3 * 0.05) and res.ucl == pytest.approx(0.65)
    assert res.p_hat == 0.2 and res.breached
    assert not st.hit_rate_pchart(wins=40, n=100, p0=0.5).breached  # inside the band
    assert not st.hit_rate_pchart(wins=2, n=20, p0=0.5).breached  # n < 50 -> never warns
    clipped = st.hit_rate_pchart(wins=1, n=10, p0=0.05)
    assert clipped.lcl == 0.0
    with pytest.raises(ValueError):
        st.hit_rate_pchart(wins=5, n=4, p0=0.5)
    with pytest.raises(ValueError):
        st.hit_rate_pchart(wins=1, n=4, p0=1.0)


def test_package_exports_public_names():
    import goldtrader.backtest as bt

    for name in ("pbo_cscv", "deflated_sharpe", "cpcv_splits", "haircut_sharpe", "cusum"):
        assert getattr(bt, name) is getattr(st, name)
