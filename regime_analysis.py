"""When did the ETH option strategies beat buy & hold, and in which market conditions?

Compares ETH variants A (original wheel), C (classic wheel + 200DMA trend) and
G (put-spread ladder above 200DMA) with buy & hold ETH, by:
  1. market phases (bull / bear legs from a 30% zig-zag on ETH)
  2. monthly regimes (ETH month return, trend, implied vol, IV vs realised vol)
  3. rolling 90-day windows (win rate vs ETH's 90-day return)
  4. up / down capture
Writes results/regimes/.
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
OUT = ROOT / "results" / "regimes"
OUT.mkdir(parents=True, exist_ok=True)

inst = L.INSTRUMENTS["ETH"]
df = L.load(inst, ROOT / "data")
end = str(df.index[-1].date())
# same definitions as improve_study.py variants A, C and G
A = L.Strat(name="A")
pick = {
    "A": A,
    "C": replace(A, name="C", call_mode="called_away", trend_ma=200, trend_exit_ma=200),
    "G": L.Strat(name="G", entry="ladder", ladder_every=7, put_dte=35, put_delta=0.25,
                 wing_delta=0.10, risk_pct=0.02, assign="cash", sell_calls=False, trend_ma=200),
}
eq = pd.DataFrame({k: L.run(df, inst, replace(v, end=end))["equity"].equity for k, v in pick.items()})
eq["BH"] = L.buy_hold(df, replace(A, end=end), inst)
d = df.loc[eq.index]
STRATS = ["A", "C", "G"]
NAMES = {"A": "A. Original wheel", "C": "C. Wheel + trend", "G": "G. Spread ladder + trend",
         "BH": "Buy & hold ETH"}

# --------------------------------------------------------------------------- #
# 1) Market phases: 30% zig-zag on ETH close
# --------------------------------------------------------------------------- #
def zigzag(s: pd.Series, thr: float = 0.30):
    """Pivots where price reverses by >= thr from the running extreme."""
    t0, v0 = s.index[0], s.iloc[0]
    piv, mode = [(t0, v0)], 0
    hi = lo = (t0, v0)
    for t, v in s.items():
        if mode >= 0 and v > hi[1]:
            hi = (t, v)
        if mode <= 0 and v < lo[1]:
            lo = (t, v)
        if mode >= 0 and v <= hi[1] * (1 - thr) and (mode == 1 or hi[1] >= v0 * (1 + thr) or mode == 0):
            if mode == 0 and hi[0] == t0:          # first leg is down
                mode, lo = -1, (t, v)
                continue
            piv.append(hi); mode, lo = -1, (t, v)
        elif mode <= 0 and v >= lo[1] * (1 + thr):
            if mode == 0 and lo[0] == t0:          # first leg is up
                mode, hi = 1, (t, v)
                continue
            piv.append(lo); mode, hi = 1, (t, v)
    piv.append((s.index[-1], s.iloc[-1]))
    return [p for k, p in enumerate(piv) if k == 0 or p[0] != piv[k - 1][0]]


piv = zigzag(d.close)
phases = []
for (t0, v0), (t1, v1) in zip(piv[:-1], piv[1:]):
    if (t1 - t0).days < 10:
        continue
    seg = eq.loc[t0:t1]
    r = seg.iloc[-1] / seg.iloc[0] - 1
    ivs = d.loc[t0:t1]
    phases.append(dict(start=t0.date(), end=t1.date(), days=(t1 - t0).days,
                       kind="Bull" if v1 > v0 else "Bear", eth_from=round(v0), eth_to=round(v1),
                       avg_iv=ivs.iv.mean(), **{f"{k}_ret": r[k] for k in STRATS + ["BH"]},
                       **{f"{k}_beat": r[k] > r["BH"] for k in STRATS}))
phases = pd.DataFrame(phases)
phases.to_csv(OUT / "phases.csv", index=False)

# Start each strategy FRESH at each turning point and hold to the end of the data
entry = []
for k_p, (t0, v0) in enumerate(piv[1:-1], start=1):
    yrs = (eq.index[-1] - t0).days / 365
    if yrs < 0.5:
        continue
    seg = pd.DataFrame({k: L.run(df, inst, replace(v, start=str(t0.date()), end=end))["equity"].equity
                        for k, v in pick.items()})
    seg["BH"] = L.buy_hold(df, replace(A, start=str(t0.date()), end=end), inst)
    cg = (seg.iloc[-1] / seg.iloc[0]) ** (1 / yrs) - 1
    dd = (seg / seg.cummax() - 1).min()
    nxt = piv[k_p + 1][1]
    entry.append(dict(start=t0.date(), eth_at_start=round(v0), start_type="Top" if nxt < v0 else "Bottom",
                      years=round(yrs, 1), **{f"{k}_cagr": cg[k] for k in STRATS + ["BH"]},
                      **{f"{k}_maxdd": dd[k] for k in STRATS + ["BH"]}))
entry = pd.DataFrame(entry)
entry.to_csv(OUT / "start_at_turning_points.csv", index=False)

# --------------------------------------------------------------------------- #
# 2) Monthly regimes
# --------------------------------------------------------------------------- #
m_eq = eq.resample("ME").last()
m_eq = pd.concat([eq.iloc[[0]], m_eq])
mr = m_eq.pct_change().dropna()
mr = mr[mr.index > eq.index[0]]
ms = d.resample("ME").first()              # conditions known at the START of the month
lr = np.log(d.close).diff()
rv_m = lr.resample("ME").std() * np.sqrt(365) * 100
feat = pd.DataFrame(index=mr.index)
feat["eth_ret"] = mr.BH
feat["trend"] = np.where(ms.close.values > ms.ma200.values, "Above 200DMA", "Below 200DMA")
feat["iv_start"] = ms.iv.values
feat["rv_month"] = rv_m.reindex(mr.index).values
feat["vrp"] = feat.iv_start - feat.rv_month           # implied minus realised (vol pts)
for k in STRATS:
    feat[f"{k}_ret"] = mr[k]
    feat[f"{k}_excess"] = mr[k] - mr.BH
feat["eth_bucket"] = pd.cut(feat.eth_ret, [-1, -0.20, -0.05, 0.05, 0.20, 10],
                            labels=["Crash (< -20%)", "Down (-20..-5%)", "Flat (±5%)",
                                    "Up (+5..+20%)", "Rally (> +20%)"])
feat["iv_bucket"] = pd.cut(feat.iv_start, [0, 60, 80, 1000],
                           labels=["Low IV (<60)", "Mid IV (60-80)", "High IV (>80)"])
feat["vrp_bucket"] = np.where(feat.vrp > 0, "IV > realised (options rich)", "IV < realised (options cheap)")
feat.to_csv(OUT / "monthly_regimes.csv")

def group_table(col):
    g = feat.groupby(col, observed=True)
    t = pd.DataFrame({"months": g.size(), "ETH avg": g.eth_ret.mean()})
    for k in STRATS:
        t[f"{k} avg"] = g[f"{k}_ret"].mean()
        t[f"{k} beat %"] = g[f"{k}_excess"].apply(lambda x: (x > 0).mean())
    return t

tables = {c: group_table(c) for c in ["eth_bucket", "trend", "iv_bucket", "vrp_bucket"]}
# combined: trend x direction
feat["eth_dir"] = np.where(feat.eth_ret >= 0, "ETH up month", "ETH down month")
tables["trend_x_dir"] = group_table(["trend", "eth_dir"])
for k, t in tables.items():
    t.to_csv(OUT / f"monthly_by_{k}.csv")

# --------------------------------------------------------------------------- #
# 3) Rolling 90-day windows
# --------------------------------------------------------------------------- #
W = 90
roll = (eq.shift(-W) / eq - 1).dropna()
roll_b = pd.cut(roll.BH, [-1, -0.30, -0.10, 0.10, 0.30, 0.60, 100],
                labels=["< -30%", "-30..-10%", "-10..+10%", "+10..+30%", "+30..+60%", "> +60%"])
rt = pd.DataFrame({"windows": roll.groupby(roll_b, observed=True).size()})
for k in STRATS:
    rt[f"{k} beat %"] = (roll[k] > roll.BH).groupby(roll_b, observed=True).mean()
    rt[f"{k} avg excess"] = (roll[k] - roll.BH).groupby(roll_b, observed=True).mean()
rt.to_csv(OUT / "rolling90_by_eth_return.csv")
# break-even: ETH 90d return below which each strategy usually wins
breakeven = {}
for k in STRATS:
    x, y = roll.BH.values, (roll[k] - roll.BH).values
    b = np.polyfit(x, y, 1)
    breakeven[k] = -b[1] / b[0]

# --------------------------------------------------------------------------- #
# 4) Up / down capture (monthly)
# --------------------------------------------------------------------------- #
cap = []
up, dn = mr.BH > 0, mr.BH < 0
for k in STRATS:
    cap.append(dict(strategy=NAMES[k],
                    up_capture=mr.loc[up, k].mean() / mr.loc[up, "BH"].mean(),
                    down_capture=mr.loc[dn, k].mean() / mr.loc[dn, "BH"].mean(),
                    beta=np.cov(mr[k], mr.BH)[0, 1] / mr.BH.var(),
                    months_beating_bh=(mr[k] > mr.BH).mean(),
                    breakeven_eth_90d=breakeven[k]))
cap = pd.DataFrame(cap)
cap.to_csv(OUT / "capture.csv", index=False)

# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
colors = {"A": "#6366f1", "C": "#0f766e", "G": "#d97706", "BH": "#64748b"}
fig, ax = plt.subplots(2, 1, figsize=(12, 8.5), sharex=True, gridspec_kw=dict(height_ratios=[1.3, 1]))
ax[0].plot(d.index, d.close, color="#334155", lw=1)
ax[0].plot(d.index, d.ma200, color="#94a3b8", lw=1, ls="--", label="200DMA")
for p in phases.itertuples():
    ax[0].axvspan(pd.Timestamp(p.start), pd.Timestamp(p.end),
                  color="#16a34a" if p.kind == "Bull" else "#dc2626", alpha=.08)
ax[0].set_yscale("log"); ax[0].set_ylabel("ETH (log)"); ax[0].legend(loc="upper left"); ax[0].grid(alpha=.3)
ax[0].set_title("ETH bull (green) / bear (red) phases, 30% zig-zag")
for k in STRATS:
    rel = eq[k] / eq.BH
    ax[1].plot(rel.index, rel / rel.iloc[0], color=colors[k], lw=1.4, label=f"{NAMES[k]} ÷ buy & hold")
ax[1].set_yscale("log"); ax[1].axhline(1, color="k", lw=.6)
ax[1].set_ylabel("Relative wealth (log)\nrising = beating B&H"); ax[1].legend(loc="upper right"); ax[1].grid(alpha=.3)
fig.tight_layout(); fig.savefig(OUT / "relative_performance.png", dpi=130)

fig, axes = plt.subplots(1, 3, figsize=(14, 4.5), sharey=True)
for a, k in zip(axes, STRATS):
    a.scatter(roll.BH * 100, roll[k] * 100, s=3, alpha=.25, color=colors[k])
    lim = [roll.BH.min() * 100, roll.BH.max() * 100]
    a.plot(lim, lim, color="k", lw=.8, label="= buy & hold")
    a.axvline(breakeven[k] * 100, color=colors[k], ls="--", lw=1, label=f"break-even ≈ {breakeven[k]:+.0%}")
    a.set_title(NAMES[k]); a.set_xlabel("ETH 90-day return (%)"); a.grid(alpha=.3); a.legend(fontsize=8)
axes[0].set_ylabel("Strategy 90-day return (%)")
fig.suptitle("Rolling 90-day returns: the strategy wins to the left of the break-even line")
fig.tight_layout(); fig.savefig(OUT / "rolling90_payoff.png", dpi=130)

# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)
p = phases.copy()
for c in [c for c in p.columns if c.endswith("_ret")]:
    p[c] = (p[c] * 100).round(1)
print(p.drop(columns=[c for c in p.columns if c.endswith("_beat")]).round(1).to_string(index=False))
for k, t in tables.items():
    print("\n==", k); print(t.round(3).to_string())
print("\n== rolling 90d"); print(rt.round(3).to_string())
print(cap.round(3).to_string(index=False))
e = entry.copy()
for c in [c for c in e.columns if "cagr" in c or "maxdd" in c]:
    e[c] = (e[c] * 100).round(1)
print(e.to_string(index=False))
