"""Trace how the ETH-only 'P4' portfolio (70% put-spread ladder + 30% trend sleeve) works."""
from pathlib import Path
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
import strategy_lab as L

ROOT = Path(__file__).parent; OUT = ROOT / "results" / "explain"; OUT.mkdir(parents=True, exist_ok=True)
inst = L.INSTRUMENTS["ETH"]; df = L.load(inst, ROOT / "data"); END = "2026-10-30"
G = L.Strat(entry="ladder", ladder_every=7, put_dte=35, put_delta=0.25, wing_delta=0.10, risk_pct=0.02,
            assign="cash", sell_calls=False, trend_ma=200, end=END)
TREND = dict(ma=(50, 100, 200), vol_target=40.0, max_weight=1.0)

res = L.run(df, inst, G)
ps = res["equity"].equity
tr = L.trend_hold(df, inst, end=END, **TREND)
p4 = L.combine({"PS": ps, "TR": tr}, {"PS": 0.7, "TR": 0.3})

# ---- every put spread: outcome at expiry ------------------------------------
lg = res["log"]; sp = lg[lg.type == "sell_put"].copy()
close = df.close
def settle(t):
    return close.loc[close.index >= t].iloc[0] if (close.index >= t).any() else np.nan
sp["s_exp"] = sp.expiry.map(settle)
sp = sp.dropna(subset=["s_exp"])
sp["width"] = sp.short_k - sp.long_k
sp["payout"] = (np.clip(sp.short_k - sp.s_exp, 0, None) - np.clip(sp.long_k - sp.s_exp, 0, None)) * sp.qty
sp["pnl"] = sp.credit - sp.payout
sp["max_loss"] = sp.width * sp.qty
sp["credit_pct_width"] = sp.credit / sp.max_loss
sp["otm_short"] = sp.short_k / sp.spot - 1
sp["otm_long"] = sp.long_k / sp.spot - 1
sp["outcome"] = np.where(sp.payout <= 0, "Full win (both expire worthless)",
                 np.where(sp.payout >= sp.max_loss * 0.999, "Max loss (ETH below long strike)", "Partial loss"))
sp["pnl_pct_equity"] = sp.pnl / sp.equity
sp.to_csv(OUT / "eth_put_spreads.csv", index=False)

summ = sp.groupby("outcome").agg(n=("pnl", "size"), avg_pnl_pct_equity=("pnl_pct_equity", "mean"))
summ["share"] = summ.n / summ.n.sum()

# ---- trend sleeve exposure ----------------------------------------------------
mas = (50, 100, 200)
frac = np.mean([(df.close > df[f"ma{m}"]).values for m in mas], axis=0)
w = pd.Series(frac * np.minimum(1, 40 / df.iv.values), index=df.index).shift(1).loc["2020-01-01":END]
frac_s = pd.Series(frac, index=df.index).shift(1).loc["2020-01-01":END]
above200 = (df.close > df.ma200).loc["2020-01-01":END]

# ---- contributions by year ------------------------------------------------
def yr(e):
    y = e.resample("YE").last(); return pd.concat([pd.Series([e.iloc[0]], index=[e.index[0]]), y]).pct_change().dropna()
contrib = pd.DataFrame({"Put-spread sleeve return": yr(ps), "Trend sleeve return": yr(tr),
                        "70% x put spreads": 0.7 * yr(ps), "30% x trend": 0.3 * yr(tr), "P4 total": yr(p4),
                        "ETH": yr(close.loc["2020-01-01":END])})
contrib.index = contrib.index.year.astype(str)
contrib.to_csv(OUT / "eth_p4_contributions.csv")

# ---- worked example: the spread sold nearest 2024-06-03 ----------------------
ex = sp.iloc[(sp.date - pd.Timestamp("2024-06-03")).abs().argmin()]
ex2 = sp[sp.outcome != "Full win (both expire worthless)"].sort_values("pnl").iloc[0]

# ---- chart -------------------------------------------------------------------
fig, ax = plt.subplots(3, 1, figsize=(12, 10), sharex=True, gridspec_kw=dict(height_ratios=[2, 1, 1.4]))
c = close.loc["2020-01-01":END]
ax[0].plot(c.index, c, color="#334155", lw=1, label="ETH")
for m, col in [(50, "#93c5fd"), (100, "#60a5fa"), (200, "#1d4ed8")]:
    ax[0].plot(c.index, df[f"ma{m}"].loc[c.index], lw=.9, color=col, label=f"{m}DMA")
for o, col in [("Full win (both expire worthless)", "#16a34a"), ("Partial loss", "#f59e0b"),
               ("Max loss (ETH below long strike)", "#dc2626")]:
    q = sp[sp.outcome == o]
    ax[0].scatter(q.date, q.short_k, s=10, color=col, label=f"Put spread: {o}", zorder=3)
ax[0].set_yscale("log"); ax[0].legend(fontsize=7, loc="upper left"); ax[0].grid(alpha=.3)
ax[0].set_title("ETH-only P4: when each sleeve is active")
ax[1].fill_between(w.index, w * 100, color="#0f766e", alpha=.5, step="post")
ax[1].set_ylabel("Trend sleeve\n% in ETH"); ax[1].grid(alpha=.3)
ax[2].plot(p4.index, p4, color="#0f766e", lw=1.4, label="P4 (70/30)")
ax[2].plot(ps.index, ps / ps.iloc[0] * 1e5, color="#d97706", lw=1, label="Put-spread sleeve alone")
ax[2].plot(tr.index, tr / tr.iloc[0] * 1e5, color="#6366f1", lw=1, label="Trend sleeve alone")
ax[2].set_yscale("log"); ax[2].legend(fontsize=8); ax[2].grid(alpha=.3); ax[2].set_ylabel("Equity")
fig.tight_layout(); fig.savefig(OUT / "eth_p4_how_it_works.png", dpi=130)

pd.set_option("display.width", 200)
print("spreads", len(sp)); print(summ.round(4))
print(sp[["otm_short", "otm_long", "credit_pct_width", "iv"]].describe().round(3))
print("avg win pnl %eq", sp[sp.pnl > 0].pnl_pct_equity.mean(), "avg loser", sp[sp.pnl < 0].pnl_pct_equity.mean(),
      "total credit", sp.credit.sum(), "total payout", sp.payout.sum(), "interest", res["agg"]["interest"])
print("time above 200DMA", above200.mean(), "trend: mean weight", w.mean(), "time fully out", (w == 0).mean(),
      "time all 3 MAs bullish", (frac_s == 1).mean(), "avg weight when all bullish", w[frac_s == 1].mean())
print("example\n", ex); print("worst\n", ex2)
print(contrib.mul(100).round(1))
print("P4", {k: round(v, 3) for k, v in L.metrics(p4, 365).items()})
# ETH-dollar exposure of P4 on average: trend weight*0.3 ; spread risk 
print("avg open spreads", res["equity"].collateral.div(res["equity"].equity).mean())
