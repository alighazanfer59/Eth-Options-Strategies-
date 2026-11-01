"""Combine the wheel / option-income sleeves with complementary sleeves so that each
market regime has something that earns.

  Regime            Who earns
  ---------------   ----------------------------------------------------
  Strong rally      Trend sleeve (ETH held while above its moving averages)
  Grind / sideways  Put-spread ladder, wheel premium
  Bear market       Bear call-spread ladder (below 200DMA), trend sleeve in T-bills
  Crash             Defined-risk spreads cap the loss; trend sleeve already out

All sleeve rules are fixed up front. Results are reported for the full period and
separately for 2020-2023 and 2024-2026 (an out-of-sample style check), plus fresh starts
on every 1 January.  Writes results/combo/.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import strategy_lab as L

ROOT = Path(__file__).parent
OUT = ROOT / "results" / "combo"
OUT.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Sleeve definitions
# --------------------------------------------------------------------------- #
WHEEL = L.Strat(name="Wheel (original)")
LADDER = dict(entry="ladder", ladder_every=7, put_dte=35, put_delta=0.25, wing_delta=0.10,
              risk_pct=0.02, assign="cash", sell_calls=False)
OPTION_SLEEVES = {
    "Wheel": WHEEL,
    "Wheel+collar": replace(WHEEL, protect_delta=0.15, protect_dte=60),
    "BullPS": L.Strat(name="Bull put spreads >200DMA", trend_ma=200, **LADDER),
    "TrendCS": L.Strat(name="Trend credit spreads", trend_ma=200, bear_calls="below_ma", **LADDER),
    "Condor": L.Strat(name="Iron-condor ladder", bear_calls="always", **LADDER),
}
TREND = dict(ma=(50, 100, 200), vol_target=40.0, max_weight=1.0)   # MA ensemble, 40% vol target
SYMS = ["ETH", "BTC", "SPY", "QQQ", "GLD"]
data = {s: L.load(L.INSTRUMENTS[s], ROOT / "data") for s in SYMS}
END = str(min(d.index[-1] for d in data.values()).date())


def sleeves_for(sym: str, start="2020-01-01") -> dict:
    inst, df = L.INSTRUMENTS[sym], data[sym]
    out = {k: L.run(df, inst, replace(v, start=start, end=END))["equity"].equity
           for k, v in OPTION_SLEEVES.items()}
    out["Trend"] = L.trend_hold(df, inst, start=start, end=END, **TREND)
    out["BuyHold"] = L.buy_hold(df, L.Strat(start=start, end=END), inst)
    out["TBills"] = L.tbills(df, L.Strat(start=start, end=END))
    return out


PORTFOLIOS = {
    "Wheel alone": {"Wheel": 1.0},
    "Trend alone": {"Trend": 1.0},
    "BullPS alone": {"BullPS": 1.0},
    "P1 Wheel 50 + Trend 50": {"Wheel": 0.5, "Trend": 0.5},
    "P2 Wheel+collar 50 + Trend 50": {"Wheel+collar": 0.5, "Trend": 0.5},
    "P3 BullPS 50 + Trend 50": {"BullPS": 0.5, "Trend": 0.5},
    "P4 BullPS 70 + Trend 30": {"BullPS": 0.7, "Trend": 0.3},
    "P5 TrendCS 50 + Trend 50": {"TrendCS": 0.5, "Trend": 0.5},
    "P6 Condor 50 + Trend 50": {"Condor": 0.5, "Trend": 0.5},
    "Ref: Trend 50 + T-bills 50": {"Trend": 0.5, "TBills": 0.5},
    "Ref: Trend 30 + T-bills 70": {"Trend": 0.3, "TBills": 0.7},
    "Buy & hold": {"BuyHold": 1.0},
}


def seg_metrics(eq: pd.Series, a: str, b: str, ppy=365):
    s = eq.loc[a:b]
    return L.metrics(s / s.iloc[0], ppy)


def summarize(curves: dict, ppy=365) -> tuple[pd.DataFrame, dict]:
    rows, ports = [], {}
    for name, w in PORTFOLIOS.items():
        e = L.combine(curves, w)
        ports[name] = e
        m = L.metrics(e, ppy)
        m1 = seg_metrics(e, "2020-01-01", "2023-12-31", ppy)
        m2 = seg_metrics(e, "2024-01-01", END, ppy)
        rows.append(dict(portfolio=name, cagr=m["cagr"], max_dd=m["max_dd"], calmar=m["calmar"],
                         sharpe=m["sharpe"], worst_year=m["worst_year"], worst_12m=m["worst_12m"],
                         pos_months=m["pct_pos_months"],
                         cagr_20_23=m1["cagr"], mdd_20_23=m1["max_dd"],
                         cagr_24_26=m2["cagr"], mdd_24_26=m2["max_dd"]))
    return pd.DataFrame(rows), ports


# --------------------------------------------------------------------------- #
# 1) ETH: sleeves and portfolios
# --------------------------------------------------------------------------- #
eth = sleeves_for("ETH")
sleeve_tbl = pd.DataFrame([dict(sleeve=k, **L.metrics(v, 365)) for k, v in eth.items()])
sleeve_tbl.to_csv(OUT / "eth_sleeves.csv", index=False)
eth_tbl, eth_ports = summarize(eth)
eth_tbl.to_csv(OUT / "eth_portfolios.csv", index=False)

# correlation of monthly returns between sleeves (diversification check)
mret = pd.DataFrame({k: v.resample("ME").last().pct_change() for k, v in eth.items()
                     if k not in ("TBills",)}).dropna()
mret.corr().to_csv(OUT / "eth_sleeve_correlation.csv")

# fresh starts every 1 January (sleeves re-run from that date)
fresh = []
for y in range(2020, 2026):
    st = f"{y}-01-01"
    c = sleeves_for("ETH", st)
    for name in ["P3 BullPS 50 + Trend 50", "P4 BullPS 70 + Trend 30", "P1 Wheel 50 + Trend 50",
                 "Ref: Trend 50 + T-bills 50", "Wheel alone", "Buy & hold"]:
        m = L.metrics(L.combine(c, PORTFOLIOS[name]), 365)
        fresh.append(dict(start=st, portfolio=name, cagr=m["cagr"], max_dd=m["max_dd"]))
fresh = pd.DataFrame(fresh)
fresh.to_csv(OUT / "eth_fresh_starts.csv", index=False)

# calendar-year returns
def yearly(e):
    y = e.resample("YE").last()
    return pd.concat([pd.Series([e.iloc[0]], index=[e.index[0]]), y]).pct_change().dropna()

yr = pd.DataFrame({k: yearly(eth_ports[k]) for k in
                   ["P3 BullPS 50 + Trend 50", "P4 BullPS 70 + Trend 30", "P2 Wheel+collar 50 + Trend 50",
                    "Ref: Trend 50 + T-bills 50", "Wheel alone", "Buy & hold"]})
yr.index = yr.index.year.astype(str)
yr.to_csv(OUT / "eth_yearly.csv")

# --------------------------------------------------------------------------- #
# 2) Same recipe on every asset, then a diversified portfolio
# --------------------------------------------------------------------------- #
allc = {"ETH": eth}
for s in SYMS[1:]:
    allc[s] = sleeves_for(s)
per_asset = []
asset_ports = {}
for s in SYMS:
    ppy = data[s].attrs["ppy"]
    for name in ["P3 BullPS 50 + Trend 50", "P4 BullPS 70 + Trend 30", "BullPS alone", "Trend alone",
                 "Ref: Trend 50 + T-bills 50", "Ref: Trend 30 + T-bills 70", "Buy & hold"]:
        e = L.combine(allc[s], PORTFOLIOS[name])
        asset_ports[(s, name)] = e
        m = L.metrics(e, ppy)
        per_asset.append(dict(symbol=s, portfolio=name, cagr=m["cagr"], max_dd=m["max_dd"],
                              calmar=m["calmar"], worst_year=m["worst_year"]))
per_asset = pd.DataFrame(per_asset)
per_asset.to_csv(OUT / "per_asset.csv", index=False)

multi_rows, multi = [], {}
for label, syms, pname in [
    ("All 5 assets, P3 (BullPS 50 + Trend 50)", SYMS, "P3 BullPS 50 + Trend 50"),
    ("All 5 assets, P4 (BullPS 70 + Trend 30)", SYMS, "P4 BullPS 70 + Trend 30"),
    ("All 5 assets, ref: Trend 50 + T-bills 50", SYMS, "Ref: Trend 50 + T-bills 50"),
    ("All 5 assets, ref: Trend 30 + T-bills 70", SYMS, "Ref: Trend 30 + T-bills 70"),
    ("ETH+BTC+SPY+GLD, P3", ["ETH", "BTC", "SPY", "GLD"], "P3 BullPS 50 + Trend 50"),
    ("ETH+BTC+SPY+GLD, P4", ["ETH", "BTC", "SPY", "GLD"], "P4 BullPS 70 + Trend 30"),
    ("ETH only, P3", ["ETH"], "P3 BullPS 50 + Trend 50"),
    ("All 5 assets, buy & hold", SYMS, "Buy & hold"),
]:
    e = L.combine({s: asset_ports[(s, pname)] for s in syms}, {s: 1 / len(syms) for s in syms})
    multi[label] = e
    m = L.metrics(e, 365)
    m1 = seg_metrics(e, "2020-01-01", "2023-12-31")
    m2 = seg_metrics(e, "2024-01-01", END)
    multi_rows.append(dict(portfolio=label, cagr=m["cagr"], max_dd=m["max_dd"], calmar=m["calmar"],
                           sharpe=m["sharpe"], worst_year=m["worst_year"], worst_12m=m["worst_12m"],
                           pos_months=m["pct_pos_months"], cagr_20_23=m1["cagr"], mdd_20_23=m1["max_dd"],
                           cagr_24_26=m2["cagr"], mdd_24_26=m2["max_dd"]))
multi_tbl = pd.DataFrame(multi_rows)
multi_tbl.to_csv(OUT / "multi_asset.csv", index=False)
myr = pd.DataFrame({k: yearly(v) for k, v in multi.items()})
myr.index = myr.index.year.astype(str)
myr.to_csv(OUT / "multi_asset_yearly.csv")

# cost stress on the recommended multi-asset portfolio (2x option bid/ask, 3x spot fees)
stress = []
for lbl, sm, fm in [("base", 1, 1), ("2x option bid/ask", 2, 1), ("2x bid/ask + 3x spot fees", 2, 3)]:
    pc = {}
    for s in SYMS:
        i0 = L.INSTRUMENTS[s]
        inst = replace(i0, spread_vol=i0.spread_vol * sm, spot_fee=i0.spot_fee * fm)
        c = {"BullPS": L.run(data[s], inst, replace(OPTION_SLEEVES["BullPS"], end=END))["equity"].equity,
             "Trend": L.trend_hold(data[s], inst, end=END, **TREND)}
        pc[s] = L.combine(c, PORTFOLIOS["P3 BullPS 50 + Trend 50"])
    e = L.combine(pc, {s: 0.2 for s in SYMS})
    m = L.metrics(e, 365)
    stress.append(dict(scenario=lbl, cagr=m["cagr"], max_dd=m["max_dd"], worst_year=m["worst_year"]))
stress = pd.DataFrame(stress)
stress.to_csv(OUT / "cost_stress.csv", index=False)

# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
fig, ax = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
for name, c in [("Wheel alone", "#6366f1"), ("P2 Wheel+collar 50 + Trend 50", "#a855f7"),
                ("P3 BullPS 50 + Trend 50", "#0f766e"), ("P4 BullPS 70 + Trend 30", "#16a34a"),
                ("Ref: Trend 50 + T-bills 50", "#f59e0b"), ("Buy & hold", "#94a3b8")]:
    e = eth_ports[name]
    ax[0].plot(e.index, e, label=name, color=c, lw=1.4)
    ax[1].plot(e.index, (e / e.cummax() - 1) * 100, color=c, lw=1.1)
ax[0].set_yscale("log"); ax[0].legend(); ax[0].grid(alpha=.3); ax[0].set_ylabel("Equity (log)")
ax[0].set_title("ETH: wheel vs combined portfolios")
ax[1].set_ylabel("Drawdown %"); ax[1].grid(alpha=.3)
fig.tight_layout(); fig.savefig(OUT / "eth_portfolios.png", dpi=130)

fig, ax = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
for name, e in multi.items():
    ax[0].plot(e.index, e, label=name, lw=1.3)
    ax[1].plot(e.index, (e / e.cummax() - 1) * 100, lw=1.1)
ax[0].set_yscale("log"); ax[0].legend(fontsize=8); ax[0].grid(alpha=.3); ax[0].set_ylabel("Equity (log)")
ax[0].set_title("Same recipe across assets")
ax[1].set_ylabel("Drawdown %"); ax[1].grid(alpha=.3)
fig.tight_layout(); fig.savefig(OUT / "multi_asset.png", dpi=130)

# --------------------------------------------------------------------------- #
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
pct = lambda d, cols: d.assign(**{c: (d[c] * 100).round(1) for c in cols})
print(pct(sleeve_tbl[["sleeve", "cagr", "max_dd", "calmar", "worst_year", "pct_pos_months"]],
          ["cagr", "max_dd", "worst_year", "pct_pos_months"]).round(2).to_string(index=False))
print(mret.corr().round(2))
cols = ["cagr", "max_dd", "worst_year", "worst_12m", "pos_months", "cagr_20_23", "mdd_20_23", "cagr_24_26", "mdd_24_26"]
print(pct(eth_tbl, cols).round(2).to_string(index=False))
print(fresh.pivot(index="portfolio", columns="start", values=["cagr", "max_dd"]).mul(100).round(1).to_string())
print(yr.mul(100).round(1).to_string())
print(pct(per_asset, ["cagr", "max_dd", "worst_year"]).round(2).to_string(index=False))
print(pct(multi_tbl, cols).round(2).to_string(index=False))
print(myr.mul(100).round(1).to_string())
print(pct(stress, ["cagr", "max_dd", "worst_year"]).to_string(index=False))
