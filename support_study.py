"""Only sell puts at strikes that are (a) just under a solid daily support zone and
(b) below the 50% level of the range between the all-time high and the last cycle low.

Tested on the ETH put-spread sleeve, the original wheel, and the ETH-only 70/30 portfolio.
Writes results/support/.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import levels as LV
import strategy_lab as L

ROOT = Path(__file__).parent
OUT = ROOT / "results" / "support"
OUT.mkdir(parents=True, exist_ok=True)
END = "2026-10-30"

inst = L.INSTRUMENTS["ETH"]
df = L.load(inst, ROOT / "data")
full = pd.read_csv(ROOT / "data/ETHUSDT_1d_binance_full.csv", parse_dates=["date"], index_col="date")
CFG = LV.SupportCfg()
RNG = LV.macro_range(full.close, 0.60)

LADDER = dict(entry="ladder", ladder_every=7, put_dte=35, put_delta=0.25, wing_delta=0.10,
              risk_pct=0.02, assign="cash", sell_calls=False, end=END)
G = L.Strat(name="G", trend_ma=200, **LADDER)
WHEEL = L.Strat(name="Wheel", end=END)
TREND = L.trend_hold(df, inst, end=END, ma=(50, 100, 200), vol_target=40.0)


def rule(support=True, discount=True, rng=RNG, cfg=CFG, width=0.15, log=None):
    return LV.make_strike_rule(full, rng, cfg, use_support=support, use_discount=discount,
                               width_frac=width, log=log)


def spread_stats(res):
    lg = res["log"]
    sp = lg[lg.type == "sell_put"].copy()
    if sp.empty or "short_k" not in sp:
        return {}
    close = df.close
    sp["s_exp"] = sp.expiry.map(lambda t: close.loc[close.index >= t].iloc[0] if (close.index >= t).any() else np.nan)
    sp = sp.dropna(subset=["s_exp"])
    pay = (np.clip(sp.short_k - sp.s_exp, 0, None) - np.clip(sp.long_k - sp.s_exp, 0, None)) * sp.qty
    pnl = sp.credit - pay
    ml = (sp.short_k - sp.long_k) * sp.qty
    return dict(spreads=len(sp), win_rate=(pnl > 0).mean(), max_loss_hits=(pay >= ml * 0.999).mean(),
                avg_credit_pct_width=(sp.credit / ml).mean(), avg_otm=(1 - sp.short_k / sp.spot).mean(),
                option_pnl=pnl.sum(), interest=res["agg"]["interest"])


rows, curves, picks = [], {}, {}
def add(name, st, kind):
    res = L.run(df, inst, st)
    e = res["equity"].equity
    curves[name] = e
    m = L.metrics(e, 365)
    m1 = L.metrics(e.loc[:"2023-12-31"], 365); m2 = L.metrics(e.loc["2024-01-01":], 365)
    row = dict(kind=kind, variant=name, cagr=m["cagr"], max_dd=m["max_dd"], calmar=m["calmar"],
               worst_year=m["worst_year"], pos_months=m["pct_pos_months"],
               cagr_20_23=m1["cagr"], cagr_24_26=m2["cagr"], mdd_24_26=m2["max_dd"],
               rule_skips=res["agg"].get("rule_skips", 0), assigned=res["agg"]["assigned"])
    row.update(spread_stats(res))
    rows.append(row)
    return res

# --------------------------------------------------------------------------- #
# 1) Put-spread sleeve variants
# --------------------------------------------------------------------------- #
log_s1, log_s2 = [], []
add("S0 Baseline: 25Δ/10Δ above 200DMA", G, "Put spreads")
add("S1 Support + below-50% strikes, above 200DMA", replace(G, strike_fn=rule(log=log_s1)), "Put spreads")
add("S2 Support + below-50% strikes, no trend filter", replace(G, trend_ma=None, strike_fn=rule(log=log_s2)), "Put spreads")
add("S3 Below-50% only (25Δ strike), no trend filter", replace(G, trend_ma=None, strike_fn=rule(support=False)), "Put spreads")
add("S4 Support only (any level), above 200DMA", replace(G, strike_fn=rule(discount=False)), "Put spreads")
add("S5 = S2 with 60-day expiry", replace(G, trend_ma=None, put_dte=60, strike_fn=rule()), "Put spreads")
# Control: same average distance as S1 (short strike ~28% below spot, 15% width) but NO levels
_s1_otm = float((1 - pd.DataFrame(log_s1).dropna(subset=["short_k"]).eval("short_k / spot")).mean())
add(f"S6 Control: fixed {_s1_otm:.0%} OTM strikes, above 200DMA (no levels)",
    replace(G, strike_fn=lambda day, S, dfn, K: (S * (1 - _s1_otm), S * (1 - _s1_otm) * 0.85)), "Put spreads")
picks["S1"] = pd.DataFrame(log_s1); picks["S2"] = pd.DataFrame(log_s2)

# --------------------------------------------------------------------------- #
# 2) Original wheel with the strike rule
# --------------------------------------------------------------------------- #
add("W0 Original wheel (5% dip, 60d 25Δ put, covered calls)", WHEEL, "Wheel")
add("W1 Wheel, strikes at support below 50% level", replace(WHEEL, strike_fn=rule()), "Wheel")
add("W2 W1 without covered calls (accumulate ETH)", replace(WHEEL, strike_fn=rule(), sell_calls=False), "Wheel")
add("W3 W1 with trend exit (sell ETH below 200DMA, called away)",
    replace(WHEEL, strike_fn=rule(), call_mode="called_away", trend_exit_ma=200), "Wheel")

# --------------------------------------------------------------------------- #
# 3) ETH-only 70/30 portfolio with each put sleeve
# --------------------------------------------------------------------------- #
for nm in ["S0 Baseline: 25Δ/10Δ above 200DMA", "S1 Support + below-50% strikes, above 200DMA",
           "S2 Support + below-50% strikes, no trend filter", "S5 = S2 with 60-day expiry"]:
    p = L.combine({"PS": curves[nm], "TR": TREND}, {"PS": 0.7, "TR": 0.3})
    lbl = "P4 70/30 with " + nm.split(" ")[0]
    curves[lbl] = p
    m = L.metrics(p, 365); m1 = L.metrics(p.loc[:"2023-12-31"], 365); m2 = L.metrics(p.loc["2024-01-01":], 365)
    rows.append(dict(kind="Portfolio", variant=lbl, cagr=m["cagr"], max_dd=m["max_dd"], calmar=m["calmar"],
                     worst_year=m["worst_year"], pos_months=m["pct_pos_months"], cagr_20_23=m1["cagr"],
                     cagr_24_26=m2["cagr"], mdd_24_26=m2["max_dd"]))
tb = L.tbills(df, L.Strat(end=END)); curves["T-bills"] = tb
m = L.metrics(tb, 365); rows.append(dict(kind="Reference", variant="T-bills only", cagr=m["cagr"], max_dd=m["max_dd"]))
res_tbl = pd.DataFrame(rows)
res_tbl.to_csv(OUT / "variants.csv", index=False)

# yearly
def yearly(e):
    y = e.resample("YE").last()
    return pd.concat([pd.Series([e.iloc[0]], index=[e.index[0]]), y]).pct_change().dropna()
yr = pd.DataFrame({k: yearly(curves[k]) for k in
                   ["S0 Baseline: 25Δ/10Δ above 200DMA", "S1 Support + below-50% strikes, above 200DMA",
                    "S2 Support + below-50% strikes, no trend filter", "P4 70/30 with S0", "P4 70/30 with S1",
                    "P4 70/30 with S2",
                    "T-bills"]})
yr.index = yr.index.year.astype(str)
yr.to_csv(OUT / "yearly.csv")

# --------------------------------------------------------------------------- #
# 4) Robustness of S2 to the choices made
# --------------------------------------------------------------------------- #
rob = []
# robustness of S1 (the best variant): change one choice at a time
grid = [("base (60% cycle, ±5d swings, 3% zone, 2 touches, 15% width)", {}, {}, 0.60, 0.15),
        ("cycle reversal 50%", {}, {}, 0.50, 0.15), ("cycle reversal 70%", {}, {}, 0.70, 0.15),
        ("swings ±10 days", {"swing": 10}, {}, 0.60, 0.15), ("swings ±3 days", {"swing": 3}, {}, 0.60, 0.15),
        ("zone tolerance 2%", {"tol": 0.02}, {}, 0.60, 0.15), ("zone tolerance 5%", {"tol": 0.05}, {}, 0.60, 0.15),
        ("3+ touches", {"min_touches": 3}, {}, 0.60, 0.15), ("1-year lookback", {"lookback": 365}, {}, 0.60, 0.15),
        ("width 10%", {}, {}, 0.60, 0.10), ("width 25%", {}, {}, 0.60, 0.25),
        ("risk 3% per spread", {}, {"risk_pct": 0.03}, 0.60, 0.15)]
for lbl, cfgk, stk, rev, wd in grid:
    rg = LV.macro_range(full.close, rev)
    st = replace(G, strike_fn=rule(rng=rg, cfg=replace(CFG, **cfgk), width=wd), **stk)
    r = L.run(df, inst, st)
    m = L.metrics(r["equity"].equity, 365)
    s = spread_stats(r)
    p = L.combine({"PS": r["equity"].equity, "TR": TREND}, {"PS": 0.7, "TR": 0.3})
    mp = L.metrics(p, 365)
    rob.append(dict(setting=lbl, sleeve_cagr=m["cagr"], sleeve_mdd=m["max_dd"], spreads=s.get("spreads", 0),
                    win_rate=s.get("win_rate", np.nan), p4_cagr=mp["cagr"], p4_mdd=mp["max_dd"]))
rob = pd.DataFrame(rob)
rob.to_csv(OUT / "robustness_S1.csv", index=False)

# execution-cost stress: deep OTM options have wide bid/ask relative to their premium
cost = []
s6_name = [k for k in curves if k.startswith("S6")][0]
for mult in [1, 2, 4]:
    i2 = replace(inst, spread_vol=inst.spread_vol * mult)
    for nm, st in [("S0 baseline", G), ("S1 support + below-50%", replace(G, strike_fn=rule())),
                   ("S6 control", replace(G, strike_fn=lambda day, S, dfn, K: (S * (1 - _s1_otm), S * (1 - _s1_otm) * 0.85)))]:
        e = L.run(df, i2, st)["equity"].equity
        m = L.metrics(e, 365)
        mp = L.metrics(L.combine({"PS": e, "TR": TREND}, {"PS": 0.7, "TR": 0.3}), 365)
        cost.append(dict(bid_ask=f"{mult}x", sleeve=nm, cagr=m["cagr"], max_dd=m["max_dd"],
                         p4_cagr=mp["cagr"], p4_mdd=mp["max_dd"]))
cost = pd.DataFrame(cost)
cost.to_csv(OUT / "cost_stress.csv", index=False)

# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #
c = df.close.loc["2020-01-01":END]
fig, ax = plt.subplots(2, 1, figsize=(12, 9), sharex=True, gridspec_kw=dict(height_ratios=[2, 1.2]))
ax[0].plot(c.index, c, color="#334155", lw=1, label="ETH")
r = RNG.loc[c.index]
ax[0].plot(r.index, r.ath, color="#16a34a", lw=1, ls="--", label="All-time high (to date)")
ax[0].plot(r.index, r.cycle_low, color="#dc2626", lw=1, ls="--", label="Last cycle low")
ax[0].plot(r.index, r.mid, color="#7c3aed", lw=1.4, label="50% level")
ax[0].fill_between(r.index, r.cycle_low, r.mid, color="#7c3aed", alpha=.06, label="Discount zone (below 50%)")
pk = picks["S2"].dropna(subset=["short_k"])
ax[0].scatter(pk.date, pk.short_k, s=10, color="#0f766e", zorder=3, label="S2 short strike (at support)")
ax[0].set_yscale("log"); ax[0].legend(fontsize=7, loc="upper left"); ax[0].grid(alpha=.3)
ax[0].set_title("Strikes at solid daily support, below the 50% level of ATH ↔ last cycle low")
for nm, col in [("S0 Baseline: 25Δ/10Δ above 200DMA", "#d97706"),
                ("S1 Support + below-50% strikes, above 200DMA", "#6366f1"),
                ("S2 Support + below-50% strikes, no trend filter", "#0f766e"), ("T-bills", "#94a3b8")]:
    e = curves[nm]; ax[1].plot(e.index, e, lw=1.3, color=col, label=nm)
ax[1].legend(fontsize=8); ax[1].grid(alpha=.3); ax[1].set_ylabel("Put-spread sleeve equity")
fig.tight_layout(); fig.savefig(OUT / "support_strikes.png", dpi=130)

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
show = res_tbl.copy()
for col in ["cagr", "max_dd", "worst_year", "pos_months", "cagr_20_23", "cagr_24_26", "mdd_24_26",
            "win_rate", "max_loss_hits", "avg_credit_pct_width", "avg_otm"]:
    if col in show: show[col] = (show[col] * 100).round(1)
print(show.drop(columns=["kind"]).round(2).to_string(index=False))
print(yr.mul(100).round(1).to_string())
print(cost.assign(**{c: (cost[c] * 100).round(1) for c in ["cagr", "max_dd", "p4_cagr", "p4_mdd"]}).to_string(index=False))
print(rob.assign(**{c: (rob[c] * 100).round(1) for c in ["sleeve_cagr", "sleeve_mdd", "win_rate", "p4_cagr", "p4_mdd"]}).to_string(index=False))
ps = picks["S1"]; print("S1 strike picks", len(ps), "avg zone touches n/a; avg delta", ps.delta.mean(), "share below mid", (ps.short_k <= ps["mid"]).mean())
ps = picks["S2"]; print("S2 weeks considered", len(ps), "traded", ps.short_k.notna().sum())
print(ps.assign(y=ps.date.dt.year).groupby("y").short_k.apply(lambda x: f"{x.notna().sum()}/{len(x)}"))
