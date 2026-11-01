"""Price levels used to pick put strikes: the macro range (ATH vs last cycle low) and
daily support zones.  Everything is causal: on date t only data up to t is used
(swing lows count only once they are confirmed, cycle pivots only once the reversal happened).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def macro_range(close: pd.Series, reversal: float = 0.60) -> pd.DataFrame:
    """ATH-to-date, last confirmed cycle low and the 50% level of that range.

    Cycle tops/bottoms come from a zig-zag with `reversal` (60% = a crypto bear/bull leg).
    A bottom is 'confirmed' when price has risen `reversal` off it; until a bottom is
    confirmed after the latest top, the running low since that top is used if it is lower
    than the previous confirmed cycle low.
    """
    ath = close.cummax()
    rows = []
    mode = 0                     # +1 looking for top, -1 looking for bottom, 0 undecided
    hi_v, lo_v = close.iloc[0], close.iloc[0]
    hi_t, lo_t = close.index[0], close.index[0]
    cycle_low, cycle_low_t = np.nan, None
    for t, v in close.items():
        if v > hi_v:
            hi_v, hi_t = v, t
        if v < lo_v:
            lo_v, lo_t = v, t
        if mode >= 0 and v <= hi_v * (1 - reversal):          # top confirmed -> look for bottom
            mode, lo_v, lo_t = -1, v, t
        elif mode <= 0 and v >= lo_v * (1 + reversal):        # bottom confirmed -> look for top
            if mode == -1 or np.isnan(cycle_low):
                cycle_low, cycle_low_t = lo_v, lo_t
            mode, hi_v, hi_t = 1, v, t
        low = lo_v if mode == -1 else cycle_low                # in a bear leg: running low
        if np.isnan(low):
            low = close.loc[:t].min()
        if mode == -1 and not np.isnan(cycle_low):
            low = min(cycle_low, lo_v)
        rows.append((t, ath.loc[t], low, cycle_low_t))
    out = pd.DataFrame(rows, columns=["date", "ath", "cycle_low", "cycle_low_date"]).set_index("date")
    out["mid"] = (out.ath + out.cycle_low) / 2
    return out


@dataclass
class SupportCfg:
    swing: int = 5              # swing low = lowest low within +-swing days (confirmed swing days later)
    lookback: int = 730         # only swing lows from the last 2 years
    tol: float = 0.03           # swing lows within 3% form one zone
    min_touches: int = 2        # "solid" = at least 2 swing lows in the zone
    buffer: float = 0.01        # short strike placed 1% under the zone's lowest touch


def swing_lows(low: pd.Series, n: int) -> pd.DataFrame:
    """Swing lows and the date each one becomes known (n days later)."""
    roll = low.rolling(2 * n + 1, center=True).min()
    piv = low[(low == roll)]
    conf = piv.index + pd.Timedelta(days=n)
    return pd.DataFrame({"level": piv.values, "pivot_date": piv.index, "known_from": conf})


def zones_at(pivots: pd.DataFrame, t: pd.Timestamp, cfg: SupportCfg) -> pd.DataFrame:
    """Support zones known on date t: clusters of swing lows."""
    p = pivots[(pivots.known_from <= t) & (pivots.pivot_date >= t - pd.Timedelta(days=cfg.lookback))]
    if p.empty:
        return pd.DataFrame(columns=["zone_low", "zone_high", "touches", "last_touch"])
    lv = p.sort_values("level")
    zones, cur = [], [lv.iloc[0]]
    for _, r in lv.iloc[1:].iterrows():
        if r.level <= cur[0].level * (1 + cfg.tol):
            cur.append(r)
        else:
            zones.append(cur); cur = [r]
    zones.append(cur)
    out = pd.DataFrame([dict(zone_low=min(x.level for x in z), zone_high=max(x.level for x in z),
                             touches=len(z), last_touch=max(x.pivot_date for x in z)) for z in zones])
    return out[out.touches >= cfg.min_touches].reset_index(drop=True)


def make_strike_rule(full: pd.DataFrame, rng: pd.DataFrame, cfg: SupportCfg,
                     use_support: bool = True, use_discount: bool = True,
                     max_delta: float = 0.30, width_frac: float = 0.15, log: list | None = None):
    """Return strike_fn(day, S, delta_fn) -> (short_K, long_K) or None (skip the trade).

    use_support : short strike sits just under a solid support zone below price
    use_discount: short strike must be <= the 50% level of the ATH/cycle-low range
    width_frac  : long strike = short strike * (1 - width_frac)
    delta_fn(K) : |delta| of a put at strike K (supplied by the engine) - strikes closer
                  than `max_delta` are rejected so trades stay out-of-the-money
    """
    piv = swing_lows(full["low"], cfg.swing)

    def fn(day, S, delta_fn, default_K):
        mid = rng.mid.asof(day)
        if use_support:
            z = zones_at(piv, day, cfg)
            z = z[z.zone_high < S]                                   # supports below price
            cands = (z.zone_low * (1 - cfg.buffer)).sort_values(ascending=False).tolist()
        else:
            cands = [default_K]
        for K in cands:
            if use_discount and K > mid:
                continue
            if delta_fn(K) > max_delta:
                continue
            if log is not None:
                log.append(dict(date=day, spot=S, short_k=K, mid=mid, ath=rng.ath.asof(day),
                                cycle_low=rng.cycle_low.asof(day), delta=delta_fn(K)))
            return K, K * (1 - width_frac)
        if log is not None:
            log.append(dict(date=day, spot=S, short_k=np.nan, mid=mid, ath=rng.ath.asof(day),
                            cycle_low=rng.cycle_low.asof(day), delta=np.nan))
        return None
    return fn
