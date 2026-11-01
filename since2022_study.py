"""Every strategy version, re-run fresh from 2022-01-01 (new capital that day) to 2026-10-30.

From 2022 all crypto options are priced from real Deribit DVOL (no pre-2021 proxy), and the
window starts near the 2021 top, so it is a much harder test than 2020-2026.
Writes results/since2022/.
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
OUT = ROOT / "results" / "since2022"
OUT.mkdir(parents=True, exist_ok=True)
START, END = "2022-01-01", "2026-10-30"

SYMS = ["ETH", "BTC", "SPY", "QQQ", "GLD"]
data = {s: L.load(L.INSTRUMENTS[s], ROOT / "data") for s in SYMS}
full = pd.read_csv(ROOT / "data/ETHUSDT_1d_binance_full.csv", parse_dates=["date"], index_col="date")
RULE = LV.make_strike_rule(full, LV.macro_range(full.close, 0.60), LV.SupportCfg())

base = dict(start=START, end=END)
WHEEL = L.Strat(**base)
LADDER = dict(entry="ladder", ladder_every=7, put_dte=35, put_delta=0.25, wing_delta=0.10,
              risk_pct=0.02, assign="cash", sell_calls=False, **base)
G = L.Strat(trend_ma=200, **LADDER)
S1_OTM = 0.277   # average distance of the support-rule strikes (SUPPORT_LEVELS.md), for the control

# ---- single sleeves (name, family, strat) ----------------------------------
SLEEVES = [
    ("A. Original wheel (5% dip puts, hold ETH, covered calls)", "Wheel family", WHEEL),
    ("A+collar. Wheel with protective puts", "Wheel family", replace(WHEEL, protect_delta=0.15)),
    ("B. Classic wheel (ETH called away)", "Wheel family", replace(WHEEL, call_mode="called_away")),
    ("C. Classic wheel + 200DMA trend filter & exit", "Wheel family",
     replace(WHEEL, call_mode="called_away", trend_ma=200, trend_exit_ma=200)),
    ("W1. Wheel with support/below-50% strikes", "Wheel family", replace(WHEEL, strike_fn=RULE)),
    ("D. Put-write ladder 100% notional", "Put selling",
     L.Strat(entry="ladder", ladder_every=7, put_dte=35, put_delta=0.25, assign="cash", sell_calls=False,
             notional_pct=0.20, max_exposure=1.0, **base)),
    ("E. Put-write ladder 50% notional", "Put selling",
     L.Strat(entry="ladder", ladder_every=7, put_dte=35, put_delta=0.25, assign="cash", sell_calls=False,
             notional_pct=0.10, max_exposure=0.5, **base)),
    ("F. Put-spread ladder 25Δ/10Δ (no filter)", "Put spreads", L.Strat(**LADDER)),
    ("G. Put-spread ladder above 200DMA", "Put spreads", G),
    ("H. Dip-triggered put spreads 60d", "Put spreads",
     L.Strat(entry="dip", put_dte=60, put_delta=0.25, wing_delta=0.10, risk_pct=0.03, assign="cash",
             sell_calls=False, **base)),
    ("S1. Support + below-50% strikes, above 200DMA", "Put spreads", replace(G, strike_fn=RULE)),
    ("S2. Support + below-50% strikes, no filter", "Put spreads", replace(G, trend_ma=None, strike_fn=RULE)),
    ("S6. Control: fixed 28% OTM, above 200DMA", "Put spreads",
     replace(G, strike_fn=lambda d, S, f, K: (S * (1 - S1_OTM), S * (1 - S1_OTM) * 0.85))),
    ("TCS. Put spreads above / call spreads below 200DMA", "Other option sleeves",
     replace(G, bear_calls="below_ma")),
    ("IC. Iron-condor ladder", "Other option sleeves", L.Strat(bear_calls="always", **LADDER)),
]
TREND_KW = dict(ma=(50, 100, 200), vol_target=40.0, max_weight=1.0)

inst, df = L.INSTRUMENTS["ETH"], data["ETH"]
curves, fam = {}, {}
for name, family, st in SLEEVES:
    curves[name] = L.run(df, inst, st)["equity"].equity
    fam[name] = family
curves["Trend sleeve (MA ensemble, 40% vol target)"] = L.trend_hold(df, inst, start=START, end=END, **TREND_KW)
fam["Trend sleeve (MA ensemble, 40% vol target)"] = "Trend"

# ---- ETH portfolios ---------------------------------------------------------
K = {k.split(".")[0]: k for k in curves}            # short keys: "A", "G", "S1", ...
TR = "Trend sleeve (MA ensemble, 40% vol target)"
tb = L.tbills(df, L.Strat(**base))
bh = L.buy_hold(df, L.Strat(**base), inst)
ports = {
    "P1. Wheel 50 + Trend 50": {K["A"]: .5, TR: .5},
    "P2. Wheel+collar 50 + Trend 50": {K["A+collar"]: .5, TR: .5},
    "P3. G 50 + Trend 50": {K["G"]: .5, TR: .5},
    "P4. G 70 + Trend 30": {K["G"]: .7, TR: .3},
    "P4-S1. Support-rule spreads 70 + Trend 30": {K["S1"]: .7, TR: .3},
    "P3-S1. Support-rule spreads 50 + Trend 50": {K["S1"]: .5, TR: .5},
}
pool = dict(curves, TBILLS=tb)
for n, w in ports.items():
    curves[n] = L.combine(pool, w)
    fam[n] = "ETH portfolios"
for n, w in {"Ref: Trend 50 + T-bills 50": {TR: .5, "TBILLS": .5},
             "Ref: Trend 30 + T-bills 70": {TR: .3, "TBILLS": .7}}.items():
    curves[n] = L.combine(pool, w); fam[n] = "References"
curves["ETH buy & hold"] = bh; fam["ETH buy & hold"] = "References"
curves["ETH 30% / T-bills 70%"] = L.mix(df, L.Strat(**base), 0.30); fam["ETH 30% / T-bills 70%"] = "References"
curves["T-bills only"] = tb; fam["T-bills only"] = "References"

# ---- multi-asset (same recipe on each asset, equal weight) -----------------
asset = {}
for s in SYMS:
    i, d = L.INSTRUMENTS[s], data[s]
    g = L.run(d, i, G)["equity"].equity
    t = L.trend_hold(d, i, start=START, end=END, **TREND_KW)
    asset[s] = dict(G=g, TR=t, BH=L.buy_hold(d, L.Strat(**base), i), TB=L.tbills(d, L.Strat(**base)))
eth_s1 = curves[K["S1"]]

def multi(syms, wg, wt, eth_sleeve=None):
    parts = {}
    for s in syms:
        g = eth_sleeve if (s == "ETH" and eth_sleeve is not None) else asset[s]["G"]
        parts[s] = L.combine({"G": g, "TR": asset[s]["TR"]}, {"G": wg, "TR": wt})
    return L.combine(parts, {s: 1 / len(syms) for s in syms})

M4 = ["ETH", "BTC", "SPY", "GLD"]
for n, c in {
    "M-P4. ETH+BTC+SPY+GLD, G 70 / Trend 30": multi(M4, .7, .3),
    "M-P3. ETH+BTC+SPY+GLD, G 50 / Trend 50": multi(M4, .5, .5),
    "M-P4-S1. same as M-P4, ETH uses support-rule spreads": multi(M4, .7, .3, eth_s1),
    "M5-P4. all 5 assets (adds QQQ), G 70 / Trend 30": multi(SYMS, .7, .3),
    "M-ref. ETH+BTC+SPY+GLD, Trend 30 / T-bills 70": L.combine(
        {s: L.combine({"TR": asset[s]["TR"], "TB": asset[s]["TB"]}, {"TR": .3, "TB": .7}) for s in M4},
        {s: .25 for s in M4}),
    "M-B&H. ETH+BTC+SPY+GLD buy & hold (monthly rebalanced)": L.combine({s: asset[s]["BH"] for s in M4},
                                                                         {s: .25 for s in M4}),
}.items():
    curves[n] = c; fam[n] = "Multi-asset"

# ---- metrics ---------------------------------------------------------------
tb_cagr = L.metrics(tb, 365)["cagr"]
def yearly(e):
    y = e.resample("YE").last()
    return pd.concat([pd.Series([e.iloc[0]], index=[e.index[0]]), y]).pct_change().dropna()

rows = []
for n, e in curves.items():
    e = e.loc[START:END]
    m = L.metrics(e, 365)
    y = yearly(e)
    rows.append(dict(family=fam[n], strategy=n, total_return=e.iloc[-1] / e.iloc[0] - 1, cagr=m["cagr"],
                     excess_vs_tbills=m["cagr"] - tb_cagr, max_dd=m["max_dd"], calmar=m["calmar"],
                     sharpe=m["sharpe"], worst_year=m["worst_year"], worst_12m=m["worst_12m"],
                     pos_months=m["pct_pos_months"], dd_recovery_days=m["dd_recovery_days"],
                     **{f"y{k.year}": v for k, v in y.items()}))
tbl = pd.DataFrame(rows)
tbl.to_csv(OUT / "all_versions_since_2022.csv", index=False)

# ---- start-date check inside the window: fresh starts every 6 months -------
starts = pd.date_range("2022-01-01", "2025-07-01", freq="6MS")
chk = []
for st in starts:
    s = str(st.date())
    b2 = dict(start=s, end=END)
    g = L.run(df, inst, replace(G, **b2))["equity"].equity
    s1 = L.run(df, inst, replace(G, strike_fn=RULE, **b2))["equity"].equity
    a = L.run(df, inst, replace(WHEEL, **b2))["equity"].equity
    t = L.trend_hold(df, inst, start=s, end=END, **TREND_KW)
    for n, c in {"A. Original wheel": a, "P4. G 70 + Trend 30": L.combine({"g": g, "t": t}, {"g": .7, "t": .3}),
                 "P4-S1. Support spreads 70 + Trend 30": L.combine({"g": s1, "t": t}, {"g": .7, "t": .3}),
                 "ETH buy & hold": L.buy_hold(df, L.Strat(**b2), inst),
                 "T-bills": L.tbills(df, L.Strat(**b2))}.items():
        m = L.metrics(c, 365)
        chk.append(dict(start=s, strategy=n, cagr=m["cagr"], max_dd=m["max_dd"]))
chk = pd.DataFrame(chk)
chk.to_csv(OUT / "fresh_starts_every_6m.csv", index=False)

# ---- charts ----------------------------------------------------------------
fig, ax = plt.subplots(figsize=(11, 7))
col = {"Wheel family": "#6366f1", "Put selling": "#a855f7", "Put spreads": "#0f766e",
       "Other option sleeves": "#f59e0b", "Trend": "#2563eb", "ETH portfolios": "#16a34a",
       "Multi-asset": "#dc2626", "References": "#64748b"}
for r in tbl.itertuples():
    ax.scatter(-r.max_dd * 100, r.cagr * 100, color=col[r.family], s=40, zorder=3)
    ax.annotate(r.strategy.split(".")[0] if "." in r.strategy.split(" ")[0] else r.strategy[:14],
                (-r.max_dd * 100, r.cagr * 100), fontsize=7, xytext=(3, 2), textcoords="offset points")
for f, c in col.items():
    ax.scatter([], [], color=c, label=f)
ax.axhline(tb_cagr * 100, color="#64748b", ls="--", lw=.8)
ax.text(1, tb_cagr * 100 + .3, "T-bills", fontsize=8, color="#64748b")
ax.set_xlabel("Max drawdown since 2022 (%)"); ax.set_ylabel("CAGR since 2022 (%)")
ax.set_title("All versions, fresh start 2022-01-01 → 2026-10-30"); ax.grid(alpha=.3); ax.legend(fontsize=8)
fig.tight_layout(); fig.savefig(OUT / "frontier_since_2022.png", dpi=130)

fig, ax = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
for n, c in [("A. Original wheel (5% dip puts, hold ETH, covered calls)", "#6366f1"),
             ("P4. G 70 + Trend 30", "#16a34a"), ("P4-S1. Support-rule spreads 70 + Trend 30", "#0f766e"),
             ("M-P4-S1. same as M-P4, ETH uses support-rule spreads", "#dc2626"),
             ("ETH buy & hold", "#94a3b8"), ("T-bills only", "#64748b")]:
    e = curves[n]
    ax[0].plot(e.index, e / e.iloc[0] * 100, label=n, color=c, lw=1.3)
    ax[1].plot(e.index, (e / e.cummax() - 1) * 100, color=c, lw=1.1)
ax[0].set_ylabel("Growth of 100"); ax[0].legend(fontsize=8); ax[0].grid(alpha=.3)
ax[0].set_title("Selected versions since 2022-01-01")
ax[1].set_ylabel("Drawdown %"); ax[1].grid(alpha=.3)
fig.tight_layout(); fig.savefig(OUT / "selected_since_2022.png", dpi=130)

pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40); pd.set_option("display.max_colwidth", 60)
show = tbl.copy()
for c in [c for c in show.columns if c not in ("family", "strategy", "calmar", "sharpe", "dd_recovery_days")]:
    show[c] = (show[c] * 100).round(1)
print(show.round(2).to_string(index=False))
print(chk.pivot(index="strategy", columns="start", values="cagr").mul(100).round(1).to_string())
print(chk.pivot(index="strategy", columns="start", values="max_dd").mul(100).round(1).to_string())
print("T-bill CAGR", round(tb_cagr * 100, 2))
