"""
ETH "Wheel Strangle" backtest (dip-triggered put selling + covered-call ladder).

Strategy (from the user brief + the Wheel Strangle deck):

Phase 1 - get paid to wait
  * Whenever ETH closes down more than `drop_trigger` (default 5%) on the day,
    sell a cash-secured put with `put_dte` (default 60) days to expiry at
    `put_delta` (default 25 delta, deck: 16-25 delta).
  * At expiry: ETH above strike -> keep premium.
               ETH below strike -> "assigned": buy the same ETH quantity at
               the strike and hold it as a long-term position.

Phase 2 - covered strangle / ladder
  * Any ETH held that is not yet covered gets a covered call with `call_dte`
    (default 14) days at `call_delta` (default 20 delta, brief: 20-25 delta).
  * Puts keep being sold on every >5% down day (the ladder), and - if
    `put_on_assignment` is on - a fresh put is sold the day a put is assigned
    ("sell another put option at the price and continue laddering").
  * Covered call ITM at expiry:
      call_mode="cash_settle" (Deribit style, default): keep the ETH, pay the
          intrinsic value in cash (selling ETH only if cash is insufficient).
      call_mode="called_away": classic wheel, ETH is sold at the strike.

Pricing
  * Black-Scholes (r = 0, crypto convention) on daily closes.
  * Volatility: Deribit ETH DVOL (30d implied vol index) from 2021-03-24.
    Before that DVOL does not exist, so a proxy is fitted on the overlap:
        ln(DVOL) = a + b ln(RV30) + c ln(RV7)
  * Skew: `put_skew` vol points added for OTM puts, `call_skew` for calls.
  * Execution: sell at (IV - spread_vol), buy back at (IV + spread_vol);
    Deribit fee 0.03% of underlying capped at 12.5% of premium, per trade;
    spot fee on ETH bought/sold.

Sizing / capital
  * Starts with `initial_capital` USD in cash.
  * Each put is sized at `put_notional_pct` of current equity (strike notional),
    and is only opened if free cash (cash minus collateral of open puts)
    can secure it -> fully cash-secured, no leverage.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from statistics import NormalDist

import numpy as np
import pandas as pd

N = NormalDist()
DAYS_PER_YEAR = 365.0


# --------------------------------------------------------------------------- #
# Black-Scholes helpers (r = 0)
# --------------------------------------------------------------------------- #
def bs_price(S: float, K: float, T: float, vol: float, kind: str) -> float:
    if T <= 0 or vol <= 0:
        return max(K - S, 0.0) if kind == "put" else max(S - K, 0.0)
    sq = vol * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * vol * vol * T) / sq
    d2 = d1 - sq
    if kind == "call":
        return S * N.cdf(d1) - K * N.cdf(d2)
    return K * N.cdf(-d2) - S * N.cdf(-d1)


def strike_for_delta(S: float, T: float, vol: float, delta: float, kind: str) -> float:
    """Strike whose BS delta (abs) equals `delta`."""
    sq = vol * math.sqrt(T)
    d1 = N.inv_cdf(delta) if kind == "call" else -N.inv_cdf(delta)
    return S * math.exp(-d1 * sq + 0.5 * vol * vol * T)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_data(price_csv: str, dvol_csv: str) -> pd.DataFrame:
    px = pd.read_csv(price_csv, parse_dates=["date"], index_col="date")
    dv = pd.read_csv(dvol_csv, parse_dates=["date"], index_col="date")["close"]
    df = px[["open", "high", "low", "close"]].copy()
    r = np.log(df.close).diff()
    df["ret"] = df.close.pct_change()
    df["rv30"] = r.rolling(30).std() * math.sqrt(DAYS_PER_YEAR) * 100
    df["rv7"] = r.rolling(7).std() * math.sqrt(DAYS_PER_YEAR) * 100
    df["dvol"] = dv

    fit = df.dropna(subset=["dvol", "rv30", "rv7"])
    X = np.c_[np.ones(len(fit)), np.log(fit.rv30), np.log(fit.rv7)]
    coef, *_ = np.linalg.lstsq(X, np.log(fit.dvol), rcond=None)
    proxy = np.exp(coef[0] + coef[1] * np.log(df.rv30) + coef[2] * np.log(df.rv7))
    df["iv_source"] = np.where(df.dvol.notna(), "DVOL", "proxy")
    df["iv"] = df.dvol.fillna(proxy)
    df.attrs["proxy_coef"] = coef.tolist()
    return df.dropna(subset=["iv"])


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    start: str = "2020-01-01"
    end: str | None = None
    initial_capital: float = 100_000.0
    drop_trigger: float = 0.05           # sell a put when daily close-to-close drop >= 5%
    put_dte: int = 60
    put_delta: float = 0.25
    call_dte: int = 14
    call_delta: float = 0.20
    put_notional_pct: float = 0.10       # strike notional of each put as % of equity
    put_on_assignment: bool = True       # immediately sell a new put when assigned
    sell_calls: bool = True              # False -> puts only (no covered calls)
    call_mode: str = "cash_settle"       # or "called_away"
    put_profit_take: float | None = None # e.g. 0.5 -> buy back put at 50% profit
    put_skew: float = 2.0                # vol points added to OTM put IV
    call_skew: float = 0.0               # vol points added to OTM call IV
    spread_vol: float = 1.0              # half bid/ask in vol points
    option_fee_rate: float = 0.0003      # Deribit: 0.03% of underlying ...
    option_fee_cap: float = 0.125        # ... capped at 12.5% of option price
    spot_fee: float = 0.001              # 0.1% spot fee on ETH buys/sells


@dataclass
class Option:
    kind: str
    open_date: pd.Timestamp
    expiry: pd.Timestamp
    strike: float
    qty: float
    premium: float           # net USD received at open (after fees)
    iv_open: float
    spot_open: float
    reason: str = ""


def _fee(cfg: Config, S: float, px: float, qty: float) -> float:
    return min(cfg.option_fee_rate * S, cfg.option_fee_cap * px) * qty


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def run(df: pd.DataFrame, cfg: Config) -> dict:
    data = df.loc[cfg.start: cfg.end]
    cash = cfg.initial_capital
    eth = 0.0
    eth_cost = 0.0                    # USD paid for ETH currently held (for cost basis)
    puts: list[Option] = []
    calls: list[Option] = []
    trades: list[dict] = []
    rows: list[dict] = []
    stats = dict(put_premium=0.0, call_premium=0.0, call_settle_paid=0.0,
                 put_buyback_paid=0.0, fees=0.0, eth_bought=0.0, usd_spent_on_eth=0.0,
                 eth_sold=0.0, usd_from_eth_sales=0.0)

    def equity(S: float, iv: float, day: pd.Timestamp) -> float:
        liab = 0.0
        for o in puts + calls:
            T = max((o.expiry - day).days, 0) / DAYS_PER_YEAR
            skew = cfg.put_skew if o.kind == "put" else cfg.call_skew
            liab += bs_price(S, o.strike, T, (iv + skew) / 100, o.kind) * o.qty
        return cash + eth * S - liab

    def collateral() -> float:
        return sum(p.strike * p.qty for p in puts)

    def sell_put(day, S, iv, reason) -> None:
        nonlocal cash
        eq = equity(S, iv, day)
        vol = (iv + cfg.put_skew - cfg.spread_vol) / 100
        T = cfg.put_dte / DAYS_PER_YEAR
        K = strike_for_delta(S, T, vol, cfg.put_delta, "put")
        qty = cfg.put_notional_pct * eq / K
        free = cash - collateral()
        if free < K * qty:                         # cash-secured only
            qty = max(free, 0.0) / K
            if qty * K < 0.25 * cfg.put_notional_pct * eq:   # too small, skip
                trades.append(dict(date=day, type="put_skipped_no_cash", spot=S, reason=reason))
                return
        px = bs_price(S, K, T, vol, "put")
        fee = _fee(cfg, S, px, qty)
        prem = px * qty - fee
        cash += prem
        stats["put_premium"] += prem
        stats["fees"] += fee
        o = Option("put", day, day + pd.Timedelta(days=cfg.put_dte), K, qty, prem, iv, S, reason)
        puts.append(o)
        trades.append(dict(date=day, type="sell_put", spot=S, strike=K, qty=qty, iv=iv,
                           expiry=o.expiry, premium=prem, premium_pct_of_strike=px / K,
                           reason=reason))

    def sell_call(day, S, iv, qty) -> None:
        nonlocal cash
        vol = (iv + cfg.call_skew - cfg.spread_vol) / 100
        T = cfg.call_dte / DAYS_PER_YEAR
        K = strike_for_delta(S, T, vol, cfg.call_delta, "call")
        px = bs_price(S, K, T, vol, "call")
        fee = _fee(cfg, S, px, qty)
        prem = px * qty - fee
        cash += prem
        stats["call_premium"] += prem
        stats["fees"] += fee
        o = Option("call", day, day + pd.Timedelta(days=cfg.call_dte), K, qty, prem, iv, S)
        calls.append(o)
        trades.append(dict(date=day, type="sell_call", spot=S, strike=K, qty=qty, iv=iv,
                           expiry=o.expiry, premium=prem, premium_pct_of_strike=px / S))

    for day, r in data.iterrows():
        S, iv = float(r.close), float(r.iv)

        # 1) expiries -------------------------------------------------------
        assigned_today = False
        for o in [p for p in puts if p.expiry <= day]:
            puts.remove(o)
            if S < o.strike:                          # assigned: buy ETH at strike
                fee = o.strike * o.qty * cfg.spot_fee
                cost = o.strike * o.qty + fee
                cash -= cost
                eth += o.qty
                eth_cost += cost
                stats["eth_bought"] += o.qty
                stats["usd_spent_on_eth"] += cost
                stats["fees"] += fee
                assigned_today = True
                trades.append(dict(date=day, type="put_assigned", spot=S, strike=o.strike,
                                   qty=o.qty, pnl=o.premium - (o.strike - S) * o.qty,
                                   premium=o.premium))
            else:
                trades.append(dict(date=day, type="put_expired_otm", spot=S, strike=o.strike,
                                   qty=o.qty, pnl=o.premium, premium=o.premium))

        for o in [c for c in calls if c.expiry <= day]:
            calls.remove(o)
            if S > o.strike:
                if cfg.call_mode == "called_away":
                    q = min(o.qty, eth)
                    proceeds = o.strike * q * (1 - cfg.spot_fee)
                    eth_cost -= eth_cost * q / eth if eth > 0 else 0
                    eth -= q
                    cash += proceeds
                    stats["eth_sold"] += q
                    stats["usd_from_eth_sales"] += proceeds
                    trades.append(dict(date=day, type="call_assigned_eth_sold", spot=S,
                                       strike=o.strike, qty=q, premium=o.premium,
                                       pnl=o.premium - (S - o.strike) * q))
                else:
                    pay = (S - o.strike) * o.qty
                    cash -= pay
                    stats["call_settle_paid"] += pay
                    trades.append(dict(date=day, type="call_itm_cash_settled", spot=S,
                                       strike=o.strike, qty=o.qty, premium=o.premium,
                                       pnl=o.premium - pay))
            else:
                trades.append(dict(date=day, type="call_expired_otm", spot=S, strike=o.strike,
                                   qty=o.qty, premium=o.premium, pnl=o.premium))

        # Cash must cover put collateral; if call settlement made it short, sell ETH.
        shortfall = collateral() - cash
        if shortfall > 0 and eth > 0:
            q = min(eth, shortfall / (S * (1 - cfg.spot_fee)))
            proceeds = q * S * (1 - cfg.spot_fee)
            eth_cost -= eth_cost * q / eth
            eth -= q
            cash += proceeds
            stats["eth_sold"] += q
            stats["usd_from_eth_sales"] += proceeds
            trades.append(dict(date=day, type="eth_sold_to_cover_cash", spot=S, qty=q))

        # 2) optional profit-take on puts ----------------------------------
        if cfg.put_profit_take:
            for o in list(puts):
                T = (o.expiry - day).days / DAYS_PER_YEAR
                buy_px = bs_price(S, o.strike, T, (iv + cfg.put_skew + cfg.spread_vol) / 100, "put")
                fee = _fee(cfg, S, buy_px, o.qty)
                cost = buy_px * o.qty + fee
                if cost <= (1 - cfg.put_profit_take) * o.premium:
                    puts.remove(o)
                    cash -= cost
                    stats["put_buyback_paid"] += cost
                    stats["fees"] += fee
                    trades.append(dict(date=day, type="put_closed_profit", spot=S, strike=o.strike,
                                       qty=o.qty, premium=o.premium, pnl=o.premium - cost))

        # 3) new puts: dip trigger + ladder on assignment --------------------
        if r.ret <= -cfg.drop_trigger:
            sell_put(day, S, iv, f"drop {r.ret:.1%}")
        elif assigned_today and cfg.put_on_assignment:
            sell_put(day, S, iv, "ladder after assignment")

        # 4) covered calls on any uncovered ETH -------------------------------
        uncovered = eth - sum(c.qty for c in calls)
        if cfg.sell_calls and uncovered > 1e-9:
            sell_call(day, S, iv, uncovered)

        eq = equity(S, iv, day)
        rows.append(dict(date=day, close=S, iv=iv, cash=cash, eth=eth,
                         eth_value=eth * S, put_collateral=collateral(),
                         open_puts=len(puts), equity=eq,
                         avg_cost=eth_cost / eth if eth > 1e-12 else np.nan))

    eq = pd.DataFrame(rows).set_index("date")
    tr = pd.DataFrame(trades)
    return dict(equity=eq, trades=tr, stats=stats, config=asdict(cfg))


# --------------------------------------------------------------------------- #
# Benchmarks
# --------------------------------------------------------------------------- #
def buy_and_hold(df: pd.DataFrame, cfg: Config) -> pd.Series:
    d = df.loc[cfg.start: cfg.end].close
    return cfg.initial_capital * (1 - cfg.spot_fee) * d / d.iloc[0]


def buy_the_dip(df: pd.DataFrame, cfg: Config) -> pd.Series:
    """Same trigger & sizing, but simply buy spot ETH instead of selling a put."""
    d = df.loc[cfg.start: cfg.end]
    cash, eth, out = cfg.initial_capital, 0.0, []
    for day, r in d.iterrows():
        S = r.close
        if r.ret <= -cfg.drop_trigger and cash > 0:
            usd = min(cash, cfg.put_notional_pct * (cash + eth * S))
            eth += usd * (1 - cfg.spot_fee) / S
            cash -= usd
        out.append(cash + eth * S)
    return pd.Series(out, index=d.index)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def metrics(eq: pd.Series) -> dict:
    eq = eq.dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / DAYS_PER_YEAR
    r = eq.pct_change().dropna()
    dd = eq / eq.cummax() - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    vol = r.std() * math.sqrt(DAYS_PER_YEAR)
    down = r[r < 0].std() * math.sqrt(DAYS_PER_YEAR)
    return dict(
        final_equity=float(eq.iloc[-1]),
        total_return=float(eq.iloc[-1] / eq.iloc[0] - 1),
        cagr=float(cagr),
        ann_vol=float(vol),
        sharpe=float(r.mean() * DAYS_PER_YEAR / vol) if vol else float("nan"),
        sortino=float(r.mean() * DAYS_PER_YEAR / down) if down else float("nan"),
        max_drawdown=float(dd.min()),
        calmar=float(cagr / -dd.min()) if dd.min() < 0 else float("nan"),
    )


def trade_summary(res: dict) -> dict:
    tr, st, eq = res["trades"], res["stats"], res["equity"]
    t = lambda k: int((tr.type == k).sum()) if len(tr) else 0
    puts_closed = t("put_assigned") + t("put_expired_otm") + t("put_closed_profit")
    calls_closed = t("call_expired_otm") + t("call_itm_cash_settled") + t("call_assigned_eth_sold")
    last = eq.iloc[-1]
    put_done = tr[tr.type.isin(["put_assigned", "put_expired_otm", "put_closed_profit"])] if len(tr) else tr
    call_done = tr[tr.type.isin(["call_expired_otm", "call_itm_cash_settled", "call_assigned_eth_sold"])] if len(tr) else tr
    return dict(
        put_leg_pnl_usd=float(put_done.pnl.sum()) if len(put_done) else 0.0,
        put_win_rate=float((put_done.pnl > 0).mean()) if len(put_done) else float("nan"),
        call_leg_pnl_usd=float(call_done.pnl.sum()) if len(call_done) else 0.0,
        puts_sold=t("sell_put"),
        puts_skipped_no_cash=t("put_skipped_no_cash"),
        puts_expired_otm=t("put_expired_otm"),
        puts_assigned=t("put_assigned"),
        puts_closed_early=t("put_closed_profit"),
        put_assignment_rate=t("put_assigned") / puts_closed if puts_closed else float("nan"),
        put_premium_usd=st["put_premium"],
        calls_sold=t("sell_call"),
        calls_finished_itm=t("call_itm_cash_settled") + t("call_assigned_eth_sold"),
        call_itm_rate=(t("call_itm_cash_settled") + t("call_assigned_eth_sold")) / calls_closed
        if calls_closed else float("nan"),
        call_premium_usd=st["call_premium"],
        call_itm_settlement_paid_usd=st["call_settle_paid"],
        fees_usd=st["fees"],
        eth_acquired=st["eth_bought"],
        avg_eth_purchase_price=st["usd_spent_on_eth"] / st["eth_bought"] if st["eth_bought"] else float("nan"),
        eth_sold=st["eth_sold"],
        eth_held_end=float(last.eth),
        cash_end=float(last.cash),
        eth_share_of_equity_end=float(last.eth_value / last.equity),
        avg_time_in_eth=float((eq.eth_value / eq.equity).mean()),
    )
