"""
Generalised short-put / wheel engine for comparing variants across instruments.

Extends wheel_backtest.py with:
  * any underlying + its 30d implied-vol index (DVOL, VIX, VXN, GVZ)
  * T-bill interest on cash (cash-secured collateral sits in T-bills) and the
    same rate in option pricing (no free lunch from r = 0)
  * a simple delta-linear vol smile per instrument
  * entry modes: "dip" (one-day drop trigger) or "ladder" (sell every N days)
  * filters: trend (close > N-day MA), IV-rank
  * risk controls: trend exit for held units, max exposure cap,
    defined-risk put credit spreads (no assignment, cash-settled)
  * assignment modes: "hold" (wheel: own the underlying) or "cash"
    (pure put-write: pay intrinsic, never own the underlying)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from statistics import NormalDist

import numpy as np
import pandas as pd

N = NormalDist()
CAL = 365.0


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def bs(S, K, T, vol, r, kind):
    if T <= 0 or vol <= 0:
        return max(K - S, 0.0) if kind == "put" else max(S - K, 0.0)
    sq = vol * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * vol * vol) * T) / sq
    d2 = d1 - sq
    df = math.exp(-r * T)
    if kind == "call":
        return S * N.cdf(d1) - K * df * N.cdf(d2)
    return K * df * N.cdf(-d2) - S * N.cdf(-d1)


def bs_delta(S, K, T, vol, r, kind):
    if T <= 0:
        return (1.0 if S > K else 0.0) if kind == "call" else (1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * vol * vol) * T) / (vol * math.sqrt(T))
    return N.cdf(d1) if kind == "call" else N.cdf(-d1)        # absolute delta


def strike_for_delta(S, T, vol, r, delta, kind):
    sq = vol * math.sqrt(T)
    d1 = N.inv_cdf(delta) if kind == "call" else -N.inv_cdf(delta)
    return S * math.exp(-d1 * sq + (r + 0.5 * vol * vol) * T)


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #
@dataclass
class Instrument:
    name: str
    price_csv: str
    iv_csv: str
    ppy: int                    # trading periods per year (365 crypto, 252 ETFs)
    atm_offset: float           # ATM IV = index + offset (vol pts)
    put_skew25: float           # 25d put IV minus ATM (vol pts), linear in delta
    call_skew25: float          # 25d call IV minus ATM
    spread_vol: float           # half bid/ask in vol pts
    fee_rate: float             # option fee as fraction of underlying ...
    fee_cap: float = 0.125      # ... capped at this fraction of premium
    spot_fee: float = 0.001
    drop_abs: float | None = None   # fixed dip trigger (ETH: 5%); else sigma-scaled


INSTRUMENTS = {
    # Crypto: Deribit DVOL; same assumptions as the original ETH study
    "ETH": Instrument("ETH", "ETHUSDT_1d_binance.csv", "ETH_DVOL_1d_deribit.csv", 365,
                      0.0, 2.0, 0.0, 1.0, 0.0003, spot_fee=0.001, drop_abs=0.05),
    "BTC": Instrument("BTC", "BTCUSDT_1d_binance.csv", "BTC_DVOL_1d_deribit.csv", 365,
                      0.0, 2.0, 0.0, 1.0, 0.0003, spot_fee=0.001),
    # US ETFs: VIX-style indices sit above ATM IV; equity put skew is steep, call skew negative
    "SPY": Instrument("SPY", "SPY_1d.csv", "VIX_1d.csv", 252,
                      -2.5, 4.5, -2.5, 0.5, 0.00002, spot_fee=0.0003),
    "QQQ": Instrument("QQQ", "QQQ_1d.csv", "VXN_1d.csv", 252,
                      -2.5, 4.0, -2.0, 0.5, 0.00002, spot_fee=0.0003),
    "GLD": Instrument("GLD", "GLD_1d.csv", "GVZ_1d.csv", 252,
                      -1.0, 1.0, 0.5, 0.5, 0.00003, spot_fee=0.0003),
}


def load(inst: Instrument, data_dir) -> pd.DataFrame:
    from pathlib import Path
    d = Path(data_dir)
    px = pd.read_csv(d / inst.price_csv, parse_dates=["date"], index_col="date")[["close"]]
    iv = pd.read_csv(d / inst.iv_csv, parse_dates=["date"], index_col="date")["close"]
    rt = pd.read_csv(d / "TBILL_3M_1d_fred.csv", parse_dates=["date"], index_col="date")["rate"]
    df = px.copy()
    lr = np.log(df.close).diff()
    df["ret"] = df.close.pct_change()
    df["rv30"] = lr.rolling(30 if inst.ppy == 365 else 21).std() * math.sqrt(inst.ppy) * 100
    df["rv7"] = lr.rolling(7 if inst.ppy == 365 else 5).std() * math.sqrt(inst.ppy) * 100
    df["ivx"] = iv.reindex(df.index)
    # Crypto vol indices start 2021-03-24 -> fit a realised-vol proxy for earlier dates
    fit = df.dropna(subset=["ivx", "rv30", "rv7"])
    X = np.c_[np.ones(len(fit)), np.log(fit.rv30), np.log(fit.rv7)]
    coef, *_ = np.linalg.lstsq(X, np.log(fit.ivx), rcond=None)
    proxy = np.exp(coef[0] + coef[1] * np.log(df.rv30) + coef[2] * np.log(df.rv7))
    # short gaps in the index (holidays / last few days) are forward-filled first
    df["ivx"] = df.ivx.ffill(limit=7)
    df["iv_proxy"] = df.ivx.isna()
    df["iv"] = df.ivx.fillna(proxy)
    df["rate"] = rt.reindex(df.index, method="ffill") / 100
    df["rate"] = df.rate.ffill().fillna(0.0)
    for n in (50, 100, 200):
        df[f"ma{n}"] = df.close.rolling(n).mean()
    w = inst.ppy
    df["ivr"] = (df.iv - df.iv.rolling(w).min()) / (df.iv.rolling(w).max() - df.iv.rolling(w).min())
    df.attrs["ppy"] = inst.ppy
    df.attrs["proxy_coef"] = coef.tolist()
    return df.dropna(subset=["iv", "ret"])


# --------------------------------------------------------------------------- #
# Strategy config
# --------------------------------------------------------------------------- #
@dataclass
class Strat:
    name: str = "wheel"
    start: str = "2020-01-01"
    end: str | None = None
    capital: float = 100_000.0
    # entries
    entry: str = "dip"               # "dip" | "ladder"
    dip_z: float = 1.4               # dip = drop >= z * daily implied sigma (if no drop_abs)
    use_drop_abs: bool = True        # use instrument.drop_abs when defined
    ladder_every: int = 7            # calendar days between sales in "ladder" mode
    put_dte: int = 60
    put_delta: float = 0.25
    wing_delta: float | None = None  # set -> put credit spread with long put at this delta
    notional_pct: float = 0.10       # naked puts: strike notional per put / equity
    risk_pct: float = 0.02           # spreads: width * qty per spread / equity
    max_exposure: float = 10.0       # cap (naked put notional + units value) / equity
    trend_ma: int | None = None      # sell puts only when close > MA
    ivr_min: float | None = None     # sell puts only when IV rank >= this
    # assignment / holding
    assign: str = "hold"             # "hold" (wheel) | "cash" (pure put-write)
    put_on_assignment: bool = True
    sell_calls: bool = True
    call_dte: int = 14
    call_delta: float = 0.20
    call_mode: str = "cash_settle"   # "cash_settle" | "called_away"
    trend_exit_ma: int | None = None # sell held units when close < MA
    cash_yield: bool = True


@dataclass
class Leg:
    kind: str
    side: int                # -1 short, +1 long
    strike: float
    qty: float
    open: pd.Timestamp
    expiry: pd.Timestamp
    group: int               # spread id (0 = naked)
    premium: float = 0.0     # net cash received (+) / paid (-) incl. fees


def run(df: pd.DataFrame, inst: Instrument, st: Strat) -> dict:
    data = df.loc[st.start: st.end]
    ppy = df.attrs["ppy"]
    cash, units, cost = st.capital, 0.0, 0.0
    legs: list[Leg] = []
    log: list[dict] = []
    rows = []
    gid = 0
    last_sale = None
    prev_day = None
    agg = dict(put_prem=0.0, put_settle=0.0, call_prem=0.0, call_settle=0.0, fees=0.0,
               interest=0.0, units_bought=0.0, usd_bought=0.0, puts=0, assigned=0, skipped=0,
               trend_exits=0)

    def iv_at(atm, kind, dabs):
        sk = inst.put_skew25 if kind == "put" else inst.call_skew25
        return max(atm + sk * (0.5 - dabs) / 0.25, 5.0)

    def leg_value(l: Leg, S, atm, r, day):
        T = max((l.expiry - day).days, 0) / CAL
        if T <= 0:
            return max(l.strike - S, 0) if l.kind == "put" else max(S - l.strike, 0)
        d = bs_delta(S, l.strike, T, atm / 100, r, l.kind)
        return bs(S, l.strike, T, iv_at(atm, l.kind, d) / 100, r, l.kind)

    def equity(S, atm, r, day):
        return cash + units * S + sum(l.side * leg_value(l, S, atm, r, day) * l.qty for l in legs)

    def collateral():
        c = 0.0
        groups = {}
        for l in legs:
            if l.kind != "put":
                continue
            if l.group == 0 and l.side < 0:
                c += l.strike * l.qty
            elif l.group:
                groups.setdefault(l.group, []).append(l)
        for g in groups.values():
            sh = [x for x in g if x.side < 0][0]
            lo = [x for x in g if x.side > 0][0]
            c += (sh.strike - lo.strike) * sh.qty
        return c

    def naked_notional():
        return sum(l.strike * l.qty for l in legs if l.kind == "put" and l.group == 0 and l.side < 0)

    def trade(kind, side, delta, dte, qty_fn, S, atm, r, day, group):
        nonlocal cash
        T = dte / CAL
        vol_mid = iv_at(atm, kind, delta)
        vol_exec = vol_mid - inst.spread_vol * (1 if side < 0 else -1)
        K = strike_for_delta(S, T, vol_mid / 100, r, delta, kind)
        qty = qty_fn(K)
        if qty <= 0:
            return None
        px = bs(S, K, T, vol_exec / 100, r, kind)
        fee = min(inst.fee_rate * S, inst.fee_cap * px) * qty
        flow = -side * px * qty - fee
        cash += flow
        agg["fees"] += fee
        l = Leg(kind, side, K, qty, day, day + pd.Timedelta(days=dte), group, flow)
        legs.append(l)
        return l

    def sell_put(S, atm, r, day, why):
        nonlocal gid, last_sale
        eq = equity(S, atm, r, day)
        free = cash - collateral()
        if st.wing_delta:
            # strikes first, then size from width
            T = st.put_dte / CAL
            K1 = strike_for_delta(S, T, iv_at(atm, "put", st.put_delta) / 100, r, st.put_delta, "put")
            K2 = strike_for_delta(S, T, iv_at(atm, "put", st.wing_delta) / 100, r, st.wing_delta, "put")
            width = K1 - K2
            qty = min(st.risk_pct * eq, max(free, 0)) / width
            if qty * width < 0.25 * st.risk_pct * eq:
                agg["skipped"] += 1
                return
            gid += 1
            sh = trade("put", -1, st.put_delta, st.put_dte, lambda K: qty, S, atm, r, day, gid)
            lo = trade("put", +1, st.wing_delta, st.put_dte, lambda K: qty, S, atm, r, day, gid)
            agg["put_prem"] += sh.premium + lo.premium
        else:
            room = st.max_exposure * eq - naked_notional() - units * S
            def q(K):
                want = st.notional_pct * eq
                return min(want, max(free, 0), max(room, 0)) / K
            T = st.put_dte / CAL
            K = strike_for_delta(S, T, iv_at(atm, "put", st.put_delta) / 100, r, st.put_delta, "put")
            if q(K) * K < 0.25 * st.notional_pct * eq:
                agg["skipped"] += 1
                return
            sh = trade("put", -1, st.put_delta, st.put_dte, q, S, atm, r, day, 0)
            agg["put_prem"] += sh.premium
        agg["puts"] += 1
        last_sale = day
        log.append(dict(date=day, type="sell_put", spot=S, why=why))

    for day, row in data.iterrows():
        S, atm, r = float(row.close), float(row.iv) + inst.atm_offset, float(row.rate)
        # interest on cash (calendar days elapsed)
        if prev_day is not None and st.cash_yield and cash > 0:
            it = cash * r * (day - prev_day).days / CAL
            cash += it
            agg["interest"] += it
        prev_day = day

        # ---- expiries ------------------------------------------------------
        assigned = False
        for l in [x for x in legs if x.expiry <= day]:
            legs.remove(l)
            intrinsic = max(l.strike - S, 0) if l.kind == "put" else max(S - l.strike, 0)
            if l.kind == "put":
                if l.group == 0 and l.side < 0 and intrinsic > 0 and st.assign == "hold":
                    c = l.strike * l.qty * (1 + inst.spot_fee)
                    cash -= c
                    units += l.qty
                    cost += c
                    agg["units_bought"] += l.qty
                    agg["usd_bought"] += c
                    agg["assigned"] += 1
                    agg["put_settle"] -= intrinsic * l.qty
                    assigned = True
                else:
                    cash += l.side * intrinsic * l.qty
                    agg["put_settle"] += l.side * intrinsic * l.qty
                    if l.side < 0 and intrinsic > 0:
                        agg["assigned"] += 1
            else:
                if intrinsic > 0 and st.call_mode == "called_away":
                    q = min(l.qty, units)
                    cash += l.strike * q * (1 - inst.spot_fee)
                    cost -= cost * q / units if units else 0
                    units -= q
                    agg["call_settle"] -= intrinsic * q
                else:
                    cash -= intrinsic * l.qty
                    agg["call_settle"] -= intrinsic * l.qty

        # ---- trend exit: dump held units (and their calls) -------------------
        if st.trend_exit_ma and units > 0 and S < row[f"ma{st.trend_exit_ma}"]:
            for l in [x for x in legs if x.kind == "call"]:
                legs.remove(l)
                T = (l.expiry - day).days / CAL
                d = bs_delta(S, l.strike, T, atm / 100, r, "call")
                px = bs(S, l.strike, T, (iv_at(atm, "call", d) + inst.spread_vol) / 100, r, "call")
                cash -= px * l.qty
                agg["call_settle"] -= px * l.qty
            cash += units * S * (1 - inst.spot_fee)
            units, cost = 0.0, 0.0
            agg["trend_exits"] += 1
            log.append(dict(date=day, type="trend_exit", spot=S))

        # cash must cover collateral -> sell units if short
        short = collateral() - cash
        if short > 0 and units > 0:
            q = min(units, short / (S * (1 - inst.spot_fee)))
            cash += q * S * (1 - inst.spot_fee)
            cost -= cost * q / units
            units -= q

        # ---- new puts ------------------------------------------------------
        ok = True
        if st.trend_ma and not (S > row[f"ma{st.trend_ma}"]):
            ok = False
        if st.ivr_min is not None and not (row.ivr >= st.ivr_min):
            ok = False
        if st.entry == "dip":
            thr = inst.drop_abs if (st.use_drop_abs and inst.drop_abs) else \
                st.dip_z * row.iv / 100 / math.sqrt(ppy)
            signal = row.ret <= -thr
        else:
            signal = last_sale is None or (day - last_sale).days >= st.ladder_every
        if ok and signal:
            sell_put(S, atm, r, day, "signal")
        elif ok and assigned and st.put_on_assignment and st.assign == "hold":
            sell_put(S, atm, r, day, "ladder after assignment")

        # ---- covered calls -----------------------------------------------
        if st.sell_calls and units > 1e-12:
            unc = units - sum(l.qty for l in legs if l.kind == "call")
            if unc > 1e-12:
                c = trade("call", -1, st.call_delta, st.call_dte, lambda K: unc, S, atm, r, day, 0)
                agg["call_prem"] += c.premium

        eq = equity(S, atm, r, day)
        rows.append(dict(date=day, close=S, equity=eq, cash=cash, units=units,
                         unit_value=units * S, put_notional=naked_notional(),
                         collateral=collateral()))

    out = pd.DataFrame(rows).set_index("date")
    return dict(equity=out, log=pd.DataFrame(log), agg=agg, strat=asdict(st))


# --------------------------------------------------------------------------- #
# Benchmarks & metrics
# --------------------------------------------------------------------------- #
def buy_hold(df, st: Strat, inst: Instrument):
    d = df.loc[st.start: st.end].close
    return st.capital * (1 - inst.spot_fee) * d / d.iloc[0]


def tbills(df, st: Strat):
    d = df.loc[st.start: st.end]
    days = d.index.to_series().diff().dt.days.fillna(0)
    return st.capital * (1 + d.rate.shift().fillna(0) * days / CAL).cumprod()


def mix(df, st: Strat, w: float):
    """w in the asset, 1-w in T-bills, rebalanced monthly."""
    d = df.loc[st.start: st.end]
    days = d.index.to_series().diff().dt.days.fillna(0)
    ra = d.close.pct_change().fillna(0)
    rc = d.rate.shift().fillna(0) * days / CAL
    eq, a, c, out, m = st.capital, st.capital * w, st.capital * (1 - w), [], None
    for i, (day, x) in enumerate(d.iterrows()):
        a *= 1 + ra.iloc[i]
        c *= 1 + rc.iloc[i]
        if m is not None and day.month != m:
            tot = a + c
            a, c = tot * w, tot * (1 - w)
        m = day.month
        out.append(a + c)
    return pd.Series(out, index=d.index)


def metrics(eq: pd.Series, ppy: int) -> dict:
    eq = eq.dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / CAL
    r = eq.pct_change().dropna()
    dd = eq / eq.cummax() - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    vol = r.std() * math.sqrt(ppy)
    yearly = eq.resample("YE").last()
    yearly = pd.concat([pd.Series([eq.iloc[0]], index=[eq.index[0]]), yearly]).pct_change().dropna()
    roll = eq.resample("ME").last()
    r12 = (roll / roll.shift(12) - 1).dropna()
    # time to recover from the max drawdown (days), NaN if never recovered
    trough = dd.idxmin()
    rec = eq[trough:][eq[trough:] >= eq[:trough].max()]
    return dict(
        cagr=cagr, max_dd=dd.min(), calmar=cagr / -dd.min() if dd.min() < 0 else np.nan,
        vol=vol, sharpe=r.mean() * ppy / vol if vol else np.nan,
        worst_year=yearly.min(), worst_12m=r12.min() if len(r12) else np.nan,
        pct_pos_months=(roll.pct_change().dropna() > 0).mean(),
        dd_recovery_days=(rec.index[0] - trough).days if len(rec) else np.nan,
        final=eq.iloc[-1],
    )
