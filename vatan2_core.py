#!/usr/bin/env python3
"""VATAN 2: pure, causal market-regime and trade-cost helpers.

OHLCV input only contains COMPLETED candles. No future lookup, no exchange keys.
Indicators here are *risk/condition checks*, not claims of measured win rate.
"""
from __future__ import annotations
import math


def _ohlc(rows):
    return [(float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def true_ranges(bars):
    result = []
    for i in range(1, len(bars)):
        prior_close = bars[i - 1][3]
        high, low = bars[i][1], bars[i][2]
        result.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    return result


def adx(rows, period=14):
    """Wilder ADX from highs/lows/closes of closed OHLCV candles."""
    bars = _ohlc(rows)
    if len(bars) < 2 * period + 2:
        return None
    tr, plus_dm, minus_dm = [], [], []
    for prev, cur in zip(bars, bars[1:]):
        up = cur[1] - prev[1]
        down = prev[2] - cur[2]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(cur[1] - cur[2], abs(cur[1] - prev[3]), abs(cur[2] - prev[3])))
    smooth_tr = sum(tr[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])
    dx_values = []
    for i in range(period - 1, len(tr)):
        if i > period - 1:
            smooth_tr = smooth_tr - smooth_tr / period + tr[i]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]
        pdi = 100 * smooth_plus / smooth_tr if smooth_tr > 0 else 0
        mdi = 100 * smooth_minus / smooth_tr if smooth_tr > 0 else 0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0
        dx_values.append(dx)
    if len(dx_values) < period:
        return None
    strength = sum(dx_values[:period]) / period
    for value in dx_values[period:]:
        strength = (strength * (period - 1) + value) / period
    return round(max(0, min(100, strength)), 2)


def choppiness(rows, period=14):
    """CHOP: 100 * log10(sum(TR) / range) / log10(period)."""
    bars = _ohlc(rows)
    if len(bars) < period + 1:
        return None
    sample = bars[-(period + 1):]
    trs = true_ranges(sample)
    high = max(x[1] for x in sample[1:])
    low = min(x[2] for x in sample[1:])
    width = high - low
    if width <= 0:
        return 100.0
    ratio = max(1, sum(trs) / width)
    return round(max(0, min(100, 100 * math.log10(ratio) / math.log10(period))), 2)


def market_regime(adx_value, chop_value, efficiency):
    """Conservative regime classification (tunable and not performance-calibrated)."""
    if any(v is None or not math.isfinite(float(v)) for v in (adx_value, chop_value, efficiency)):
        return 'VERI_EKSIK'
    if chop_value >= 61.8 or adx_value < 18:
        return 'YATAY'
    if adx_value >= 25 and chop_value < 55 and efficiency >= 0.32:
        return 'TREND'
    return 'GECIS'


def expected_net_rr(entry, stop, target, side, fee_each_pct=0.04, slip_each_pct=0.02):
    """Simplified fee/slip-adjusted R:R (not a market-impact execution guarantee)."""
    if any(not math.isfinite(float(x)) for x in (entry, stop, target)) or min(entry, stop, target) <= 0:
        return None
    if side == 'LONG' and not (stop < entry < target):
        return None
    if side == 'SHORT' and not (target < entry < stop):
        return None
    friction = entry * 2 * (fee_each_pct + slip_each_pct) / 100
    gross_risk = abs(entry - stop)
    gross_win = abs(target - entry)
    if gross_risk <= 0:
        return None
    return round((gross_win - friction) / (gross_risk + friction), 4)


def self_test():
    trend = []
    for i in range(100):
        center = 100 + i * .42
        trend.append([i, center - .15, center + .3, center - .4, center, 10, i + 1, 1000])
    sideways = []
    for i in range(100):
        center = 100 + math.sin(i * 1.1) * .5
        sideways.append([i, center - .15, center + .8, center - .8, center, 10, i + 1, 1000])
    assert adx(trend) is not None and adx(trend) > 25, adx(trend)
    assert choppiness(sideways) is not None and choppiness(sideways) > choppiness(trend)
    assert market_regime(28, 43, .6) == 'TREND'
    assert market_regime(12, 65, .07) == 'YATAY'
    assert market_regime(None, 50, .5) == 'VERI_EKSIK'
    assert expected_net_rr(100, 99, 101.8, 'LONG') < 1.8
    assert expected_net_rr(100, 101, 98.2, 'SHORT') < 1.8
    assert expected_net_rr(100, 101, 102, 'LONG') is None
    print('CORE_TEST_OK: ADX, CHOP, rejim, net R/R')

if __name__ == '__main__':
    self_test()
