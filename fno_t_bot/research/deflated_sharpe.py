# -*- coding: utf-8 -*-
"""
Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014)
=====================================================

Why this exists here
--------------------
Over one week this project tested a dozen-plus configuration variants against
samples of 5-132 trades: chase thresholds (0.75 / 0.93 / off / 0.40), TREND
retrace bands, minimum-leg multiples, per-path scorer weights, UNIFIED
thresholds (55 / 40 / observe-only), REV component removals. Each was adopted
because it improved the sample it was measured on.

That is textbook selection bias. If you try N variants and keep the best, the
best one looks good even when every variant has zero true edge. DSR corrects
the observed Sharpe for (a) how many things you tried, (b) skew, (c) fat tails,
(d) sample length -- and returns a PROBABILITY that the true Sharpe exceeds a
benchmark, rather than a point estimate that flatters itself.

Reference: Bailey, D. and Lopez de Prado, M. (2014), "The Deflated Sharpe
Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality",
Journal of Portfolio Management 40(5), 94-107.
"""
from __future__ import annotations
import math
import statistics as _st

EULER_MASCHERONI = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation).

    Accurate to ~1.15e-9, which is far beyond what matters here. Written out
    rather than pulling scipy so this module has no dependency beyond stdlib.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0,1), got {p}")
    a = [-3.969683028665376e+01,  2.209460984245205e+02, -2.759285104469687e+02,
          1.383577518672690e+02, -3.066479806614716e+01,  2.506628277459239e+00]
    b = [-5.447609879822406e+01,  1.615858368580409e+02, -1.556989798598866e+02,
          6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00,  4.374664141464968e+00,  2.938163982698783e+00]
    d = [ 7.784695709041462e-03,  3.224671290700398e-01,  2.445134137142996e+00,
          3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def sharpe(returns: list[float]) -> float:
    """Per-trade (NOT annualised) Sharpe. Annualising a 5-trade sample would
    be the kind of flattery this module exists to strip out."""
    n = len(returns)
    if n < 2:
        return 0.0
    sd = _st.pstdev(returns)
    if sd == 0:
        return 0.0
    return _st.mean(returns) / sd


def _skew(x: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    m = _st.mean(x); sd = _st.pstdev(x)
    if sd == 0:
        return 0.0
    return sum(((v - m) / sd) ** 3 for v in x) / n


def _kurtosis(x: list[float]) -> float:
    """Non-excess (normal == 3.0), which is the convention the DSR formula uses."""
    n = len(x)
    if n < 4:
        return 3.0
    m = _st.mean(x); sd = _st.pstdev(x)
    if sd == 0:
        return 3.0
    return sum(((v - m) / sd) ** 4 for v in x) / n


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected MAXIMUM Sharpe across n_trials independent strategies that all
    truly have zero edge. This is the bar a real strategy has to clear -- not
    zero. Grows with the number of things you tried.
    """
    if n_trials < 2:
        return 0.0
    sd = math.sqrt(max(sr_variance, 1e-12))
    g = EULER_MASCHERONI
    return sd * ((1 - g) * _norm_ppf(1 - 1.0 / n_trials)
                 + g * _norm_ppf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe(returns: list[float],
                    n_trials: int,
                    sr_variance: float | None = None,
                    sr_benchmark: float | None = None) -> dict:
    """
    Returns dict with observed SR, the selection-bias-adjusted threshold, and
    DSR = P(true Sharpe > benchmark).

    sr_variance : variance of Sharpe ACROSS the trials you ran. If unknown,
                  the null-hypothesis value 1/(T-1) is used, which is the
                  conservative-in-the-wrong-direction choice: it UNDERSTATES
                  the threshold, so a failing DSR here is doubly damning.
    """
    T = len(returns)
    out = {'n': T, 'n_trials': n_trials, 'sharpe': 0.0, 'sr0': 0.0,
           'dsr': 0.0, 'skew': 0.0, 'kurtosis': 3.0, 'verdict': ''}
    if T < 3:
        out['verdict'] = f'sample too small (n={T}) — DSR undefined'
        return out

    sr = sharpe(returns)
    g3 = _skew(returns)
    g4 = _kurtosis(returns)
    if sr_variance is None:
        sr_variance = 1.0 / max(T - 1, 1)
    sr0 = sr_benchmark if sr_benchmark is not None \
        else expected_max_sharpe(n_trials, sr_variance)

    denom = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * (sr ** 2)
    if denom <= 0:
        out.update(sharpe=sr, sr0=sr0, skew=g3, kurtosis=g4,
                   verdict='non-normality makes DSR undefined here')
        return out
    z = (sr - sr0) * math.sqrt(T - 1) / math.sqrt(denom)
    dsr = _norm_cdf(z)

    if dsr >= 0.95:   v = 'PASSES — edge survives selection bias'
    elif dsr >= 0.80: v = 'weak — suggestive, not established'
    elif dsr >= 0.50: v = 'FAILS — indistinguishable from the best of N random tries'
    else:             v = 'FAILS badly — worse than the expected best random try'

    out.update(sharpe=sr, sr0=sr0, dsr=dsr, skew=g3, kurtosis=g4, verdict=v)
    return out


def min_track_record_length(returns: list[float], sr_benchmark: float = 0.0,
                            confidence: float = 0.95) -> float:
    """How many trades before the observed Sharpe would be significant.

    Note this depends on sd/mean, so it is INVARIANT to a uniform mis-pricing
    of P&L -- which is why it stays valid for this project's Black-Scholes era.
    """
    T = len(returns)
    if T < 3:
        return float('inf')
    sr = sharpe(returns)
    if sr <= sr_benchmark:
        return float('inf')
    g3 = _skew(returns); g4 = _kurtosis(returns)
    z = _norm_ppf(confidence)
    denom = (sr - sr_benchmark) ** 2
    if denom <= 0:
        return float('inf')
    return 1 + (1 - g3 * sr + ((g4 - 1) / 4) * sr ** 2) * (z / (sr - sr_benchmark)) ** 2
