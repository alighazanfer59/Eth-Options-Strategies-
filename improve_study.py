"""Search for a lower-drawdown version of the strategy and a better underlying.

Variants are fixed up front (not optimised) and run on ETH, BTC, SPY, QQQ, GLD.
Writes results/improve/.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import strategy_lab as L

ROOT = Path(__file__).parent
OUT = ROOT / "results" / "improve"
OUT.mkdir(parents=True, exist_ok=True)

ORIG = L.Strat(name="A. Original wheel (hold ETH, cash-settled calls)")
VARIANTS = [
    ORIG,
    replace(ORIG, name="B. Classic wheel (called away)", call_mode="called_away"),
    replace(ORIG, name="C. Classic wheel + 200DMA trend filter & exit", call_mode="called_away",
            trend_ma=200, trend_exit_ma=200),
    L.Strat(name="D. Put-write ladder, 100% notional (25Δ 35d weekly)", entry="ladder",
            ladder_every=7, put_dte=35, put_delta=0.25, assign="cash", sell_calls=False,
            notional_pct=0.20, max_exposure=1.0),
    L.Strat(name="E. Put-write ladder, 50% notional", entry="ladder", ladder_every=7, put_dte=35,
            put_delta=0.25, assign="cash", sell_calls=False, notional_pct=0.10, max_exposure=0.5),
    L.Strat(name="F. Put-spread ladder 25Δ/10Δ, 2% risk each", entry="ladder", ladder_every=7,
            put_dte=35, put_delta=0.25, wing_delta=0.10, risk_pct=0.02, assign="cash",
            sell_calls=False),
    L.Strat(name="G. F + only above 200DMA", entry="ladder", ladder_every=7, put_dte=35,
            put_delta=0.25, wing_delta=0.10, risk_pct=0.02, assign="cash", sell_calls=False,
            trend_ma=200),
    L.Strat(name="H. Dip-triggered put spreads 60d 25Δ/10Δ, 3% risk", entry="dip", put_dte=60,
            put_delta=0.25, wing_delta=0.10, risk_pct=0.03, assign="cash", sell_calls=False),
]
SYMS = ["ETH", "BTC", "SPY", "QQQ", "GLD"]

data = {s: L.load(L.INSTRUMENTS[s], ROOT / "data") for s in SYMS}
end = min(d.index[-1] for d in data.values())   # common end date
rows, curves = [], {}
for s in SYMS:
    inst, df = L.INSTRUMENTS[s], data[s]
    ppy = df.attrs["ppy"]
    for v in VARIANTS:
        st = replace(v, end=str(end.date()))
        res = L.run(df, inst, st)
        m = L.metrics(res["equity"].equity, ppy)
        a = res["agg"]
        rows.append(dict(symbol=s, strategy=v.name, **m, puts=a["puts"], assigned=a["assigned"],
                         skipped=a["skipped"], interest=a["interest"],
                         avg_exposure=float(((res["equity"].unit_value + res["equity"].put_notional)
                                             / res["equity"].equity).mean())))
        curves[(s, v.name)] = res["equity"].equity
    base = replace(ORIG, end=str(end.date()))
    for nm, ser in [("Buy & hold", L.buy_hold(df, base, inst)),
                    ("30% asset / 70% T-bills", L.mix(df, base, 0.30)),
                    ("T-bills only", L.tbills(df, base))]:
        rows.append(dict(symbol=s, strategy="Bench: " + nm, **L.metrics(ser, ppy)))
        curves[(s, "Bench: " + nm)] = ser

res = pd.DataFrame(rows)
res.to_csv(OUT / "all_variants.csv", index=False)

# --------------------------------------------------------------------------- #
# Robustness for spread ladders: start dates and sizing
# --------------------------------------------------------------------------- #
rob = []
for s in SYMS:
    inst, df = L.INSTRUMENTS[s], data[s]
    for start in ["2020-01-01", "2021-03-24", "2022-01-01", "2023-01-01", "2024-01-01"]:
        for v in [VARIANTS[5], VARIANTS[6], VARIANTS[4]]:
            r = L.run(df, inst, replace(v, start=start, end=str(end.date())))
            m = L.metrics(r["equity"].equity, df.attrs["ppy"])
            rob.append(dict(symbol=s, strategy=v.name[:2], start=start, cagr=m["cagr"],
                            max_dd=m["max_dd"], calmar=m["calmar"]))
rob = pd.DataFrame(rob)
rob.to_csv(OUT / "start_date_robustness.csv", index=False)

size = []
for s in SYMS:
    inst, df = L.INSTRUMENTS[s], data[s]
    for rp in [0.01, 0.02, 0.03, 0.05]:
        for v in [VARIANTS[5], VARIANTS[6]]:
            r = L.run(df, inst, replace(v, risk_pct=rp, end=str(end.date())))
            m = L.metrics(r["equity"].equity, df.attrs["ppy"])
            size.append(dict(symbol=s, strategy=v.name[:2], risk_pct=rp, cagr=m["cagr"],
                             max_dd=m["max_dd"], calmar=m["calmar"], worst_year=m["worst_year"]))
    for npct in [0.05, 0.10, 0.20]:
        v = VARIANTS[4]
        r = L.run(df, inst, replace(v, notional_pct=npct, max_exposure=npct * 5, end=str(end.date())))
        m = L.metrics(r["equity"].equity, df.attrs["ppy"])
        size.append(dict(symbol=s, strategy="E", risk_pct=npct * 5, cagr=m["cagr"],
                         max_dd=m["max_dd"], calmar=m["calmar"], worst_year=m["worst_year"]))
size = pd.DataFrame(size)
size.to_csv(OUT / "sizing.csv", index=False)

# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
colors = dict(ETH="#6366f1", BTC="#f59e0b", SPY="#0f766e", QQQ="#0284c7", GLD="#ca8a04")
fig, ax = plt.subplots(figsize=(11, 7))
for _, r in res.iterrows():
    if r.strategy.startswith("Bench: T-bills"):
        continue
    mk = "x" if r.strategy.startswith("Bench") else "o"
    ax.scatter(-r.max_dd * 100, r.cagr * 100, color=colors[r.symbol], marker=mk, s=40, alpha=.85)
    lab = r.strategy[:1] if not r.strategy.startswith("Bench") else ("BH" if "Buy" in r.strategy else "30/70")
    ax.annotate(f"{r.symbol}-{lab}", (-r.max_dd * 100, r.cagr * 100), fontsize=7,
                xytext=(3, 2), textcoords="offset points")
ax.axvspan(0, 15, color="#16a34a", alpha=.06)
ax.set_xlabel("Max drawdown (%)"); ax.set_ylabel("CAGR (%)")
ax.set_title(f"Return vs drawdown, 2020-01-01 → {end.date()} (o = option strategy, x = benchmark)")
ax.grid(alpha=.3)
for s, c in colors.items():
    ax.scatter([], [], color=c, label=s)
ax.legend(loc="upper left")
fig.tight_layout(); fig.savefig(OUT / "frontier.png", dpi=130)

fig, axes = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
pick = [("SPY", VARIANTS[6].name), ("SPY", VARIANTS[5].name), ("SPY", VARIANTS[4].name),
        ("ETH", VARIANTS[6].name), ("BTC", VARIANTS[6].name), ("GLD", VARIANTS[6].name),
        ("SPY", "Bench: 30% asset / 70% T-bills"), ("SPY", "Bench: T-bills only")]
for s, n in pick:
    e = curves[(s, n)]
    lbl = f"{s} {n[:2] if not n.startswith('Bench') else n[7:]}"
    axes[0].plot(e.index, e, lw=1.3, label=lbl)
    axes[1].plot(e.index, (e / e.cummax() - 1) * 100, lw=1.1, label=lbl)
axes[0].set_ylabel("Equity (USD)"); axes[0].legend(fontsize=8, ncol=2); axes[0].grid(alpha=.3)
axes[0].set_title("Low-drawdown candidates")
axes[1].set_ylabel("Drawdown %"); axes[1].grid(alpha=.3)
fig.tight_layout(); fig.savefig(OUT / "low_dd_candidates.png", dpi=130)

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 20); pd.set_option("display.max_rows", 200)
show = res[["symbol", "strategy", "cagr", "max_dd", "calmar", "sharpe", "worst_year", "worst_12m",
            "dd_recovery_days", "avg_exposure", "puts", "assigned"]].copy()
for c in ["cagr", "max_dd", "worst_year", "worst_12m", "avg_exposure"]:
    show[c] = (show[c] * 100).round(1)
for c in ["calmar", "sharpe"]:
    show[c] = show[c].round(2)
print(show.to_string(index=False))
print(rob.pivot_table(index=["symbol", "strategy"], columns="start", values=["cagr", "max_dd"]).mul(100).round(1).to_string())
print(size.assign(cagr=size.cagr * 100, max_dd=size.max_dd * 100, worst_year=size.worst_year * 100).round(2).to_string(index=False))
print("end", end.date())

# --------------------------------------------------------------------------- #
# Fair comparison 1: an asset/T-bill mix scaled to the SAME max drawdown
# --------------------------------------------------------------------------- #
G = VARIANTS[6]
matched = []
for s in SYMS:
    inst, df = L.INSTRUMENTS[s], data[s]
    ppy = df.attrs["ppy"]
    base = replace(ORIG, end=str(end.date()))
    gm = L.metrics(curves[(s, G.name)], ppy)
    lo, hi = 0.0, 1.0
    for _ in range(25):                       # bisection on weight
        w = (lo + hi) / 2
        mm = L.metrics(L.mix(df, base, w), ppy)
        lo, hi = (w, hi) if mm["max_dd"] > gm["max_dd"] else (lo, w)
    matched.append(dict(symbol=s, G_cagr=gm["cagr"], G_max_dd=gm["max_dd"], mix_weight=w,
                        mix_cagr=mm["cagr"], mix_max_dd=mm["max_dd"],
                        tbill_cagr=L.metrics(L.tbills(df, base), ppy)["cagr"]))
matched = pd.DataFrame(matched)
matched.to_csv(OUT / "drawdown_matched_mix.csv", index=False)

# T-bill CAGR for each start date (excess-return view)
tb = {st: L.metrics(L.tbills(data["SPY"], replace(ORIG, start=st, end=str(end.date()))), 252)["cagr"]
      for st in rob.start.unique()}
rob["tbill_cagr"] = rob.start.map(tb)
rob["excess_cagr"] = rob.cagr - rob.tbill_cagr
rob.to_csv(OUT / "start_date_robustness.csv", index=False)

# --------------------------------------------------------------------------- #
# Fair comparison 2: diversified basket of G (2% risk) sleeves, equal capital, no rebalancing
# --------------------------------------------------------------------------- #
def basket(syms, strat_name=G.name):
    idx = sorted(set().union(*[curves[(s, strat_name)].index for s in syms]))
    parts = [curves[(s, strat_name)].reindex(idx).ffill().bfill() / len(syms) for s in syms]
    return sum(parts)

baskets = {
    "G basket: SPY+QQQ+GLD+BTC+ETH": ["SPY", "QQQ", "GLD", "BTC", "ETH"],
    "G basket: SPY+GLD+BTC": ["SPY", "GLD", "BTC"],
    "G basket: SPY+GLD": ["SPY", "GLD"],
    "G basket: BTC+ETH": ["BTC", "ETH"],
}
brow = []
for nm, syms in baskets.items():
    e = basket(syms)
    curves[("BASKET", nm)] = e
    brow.append(dict(basket=nm, **L.metrics(e, 365)))
brow = pd.DataFrame(brow)
brow.to_csv(OUT / "baskets.csv", index=False)

fig, ax = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
for nm in baskets:
    e = curves[("BASKET", nm)]
    ax[0].plot(e.index, e, lw=1.3, label=nm)
    ax[1].plot(e.index, (e / e.cummax() - 1) * 100, lw=1.1, label=nm)
tbc = L.tbills(data["BTC"], replace(ORIG, end=str(end.date())))
ax[0].plot(tbc.index, tbc, lw=1, ls="--", color="#64748b", label="T-bills only")
ax[0].legend(fontsize=8); ax[0].grid(alpha=.3); ax[0].set_ylabel("Equity (USD)")
ax[0].set_title("Diversified baskets of strategy G (put-spread ladder above 200DMA, 2% risk)")
ax[1].grid(alpha=.3); ax[1].set_ylabel("Drawdown %")
fig.tight_layout(); fig.savefig(OUT / "baskets.png", dpi=130)

print(matched.round(3).to_string(index=False))
print(rob[rob.strategy == "G."].pivot(index="symbol", columns="start", values="excess_cagr").mul(100).round(1))
print(brow.round(3).to_string(index=False))

# Fair comparison 3: the same baskets built from asset/T-bill mixes, scaled to the same drawdown
mrow = []
for nm, syms in baskets.items():
    target = L.metrics(curves[("BASKET", nm)], 365)
    lo, hi = 0.0, 1.0
    for _ in range(20):
        w = (lo + hi) / 2
        ms = [L.mix(data[s], replace(ORIG, end=str(end.date())), w) for s in syms]
        idx = sorted(set().union(*[m.index for m in ms]))
        e = sum(m.reindex(idx).ffill().bfill() / len(syms) for m in ms)
        mm = L.metrics(e, 365)
        lo, hi = (w, hi) if mm["max_dd"] > target["max_dd"] else (lo, w)
    mrow.append(dict(basket=nm, options_cagr=target["cagr"], options_max_dd=target["max_dd"],
                     options_worst_year=target["worst_year"], mix_weight_each=w,
                     mix_cagr=mm["cagr"], mix_max_dd=mm["max_dd"], mix_worst_year=mm["worst_year"]))
mrow = pd.DataFrame(mrow)
mrow.to_csv(OUT / "baskets_vs_matched_mix.csv", index=False)
print(mrow.round(3).to_string(index=False))

# --------------------------------------------------------------------------- #
# Cost / model stress on the 5-asset G basket
# --------------------------------------------------------------------------- #
stress = []
for label, skew_mult, spread_mult in [("base assumptions", 1.0, 1.0), ("2x bid/ask", 1.0, 2.0),
                                      ("1.5x put skew (pricier wings)", 1.5, 1.0),
                                      ("both", 1.5, 2.0), ("3x bid/ask + 2x skew", 2.0, 3.0)]:
    cs = []
    for s in SYMS:
        i0 = L.INSTRUMENTS[s]
        inst = replace(i0, put_skew25=i0.put_skew25 * skew_mult, spread_vol=i0.spread_vol * spread_mult)
        cs.append(L.run(data[s], inst, replace(G, end=str(end.date())))["equity"].equity)
    idx = sorted(set().union(*[c.index for c in cs]))
    e = sum(c.reindex(idx).ffill().bfill() / len(cs) for c in cs)
    m = L.metrics(e, 365)
    stress.append(dict(scenario=label, cagr=m["cagr"], max_dd=m["max_dd"], worst_year=m["worst_year"],
                       calmar=m["calmar"]))
stress = pd.DataFrame(stress)
stress.to_csv(OUT / "basket_cost_stress.csv", index=False)
print(stress.round(3).to_string(index=False))
