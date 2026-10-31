"""Run the ETH Wheel-Strangle backtest, variants, sensitivity grid, and write results/."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

import wheel_backtest as wb

ROOT = Path(__file__).parent
OUT = ROOT / "results"
OUT.mkdir(exist_ok=True)

df = wb.load_data(ROOT / "data/ETHUSDT_1d_binance.csv", ROOT / "data/ETH_DVOL_1d_deribit.csv")
BASE = wb.Config()


def summarize(name: str, cfg: wb.Config) -> tuple[dict, dict]:
    res = wb.run(df, cfg)
    row = {"strategy": name, **wb.metrics(res["equity"].equity), **wb.trade_summary(res)}
    return row, res


def fmt_pct(x):
    return f"{x:.1%}"


# --------------------------------------------------------------------------- #
# 1) Base case + variants + benchmarks
# --------------------------------------------------------------------------- #
variants = {
    "Base: 5% dip -> 60d 25Δ put; 14d 20Δ covered calls (cash-settled), ladder": BASE,
    "Classic wheel: calls ITM -> ETH called away at strike": replace(BASE, call_mode="called_away"),
    "Puts only (no covered calls), hold assigned ETH": replace(BASE, sell_calls=False),
    "Base + 14d 25Δ calls": replace(BASE, call_delta=0.25),
    "Base + close puts at 50% profit": replace(BASE, put_profit_take=0.5),
    "Base without extra put on assignment": replace(BASE, put_on_assignment=False),
}
rows, results = [], {}
for name, cfg in variants.items():
    row, res = summarize(name, cfg)
    rows.append(row)
    results[name] = res

bh = wb.buy_and_hold(df, BASE)
dip = wb.buy_the_dip(df, BASE)
rows.append({"strategy": "Benchmark: buy & hold ETH (all-in 2020-01-01)", **wb.metrics(bh)})
rows.append({"strategy": "Benchmark: buy spot on every 5% dip (same sizing, no options)", **wb.metrics(dip)})
summary = pd.DataFrame(rows)
summary.to_csv(OUT / "summary_variants.csv", index=False)

base_name = next(iter(variants))
base = results[base_name]
base["trades"].to_csv(OUT / "trades_base.csv", index=False)
base["equity"].to_csv(OUT / "equity_base.csv")

# --------------------------------------------------------------------------- #
# 2) Calendar-year returns
# --------------------------------------------------------------------------- #
def yearly(s: pd.Series) -> pd.Series:
    y = s.resample("YE").last()
    first = pd.Series([s.iloc[0]], index=[s.index[0] - pd.Timedelta(days=1)])
    return pd.concat([first, y]).pct_change().dropna()

yr = pd.DataFrame({
    "Wheel (base)": yearly(base["equity"].equity),
    "Puts only": yearly(results["Puts only (no covered calls), hold assigned ETH"]["equity"].equity),
    "Buy the dip": yearly(dip),
    "Buy & hold": yearly(bh),
    "ETH price": yearly(df.loc[BASE.start:].close),
})
yr.index = yr.index.year.astype(str)
yr.to_csv(OUT / "yearly_returns.csv")

# --------------------------------------------------------------------------- #
# 3) Different start dates (robustness; 2021-03-24 = first real DVOL day)
# --------------------------------------------------------------------------- #
starts = ["2020-01-01", "2021-03-24", "2022-01-01", "2023-01-01", "2024-01-01"]
srows = []
for s in starts:
    c = replace(BASE, start=s)
    m = wb.metrics(wb.run(df, c)["equity"].equity)
    mp = wb.metrics(wb.run(df, replace(c, sell_calls=False))["equity"].equity)
    mb = wb.metrics(wb.buy_and_hold(df, c))
    md = wb.metrics(wb.buy_the_dip(df, c))
    srows.append(dict(start=s,
                      wheel_cagr=m["cagr"], wheel_mdd=m["max_drawdown"], wheel_sharpe=m["sharpe"],
                      puts_only_cagr=mp["cagr"], puts_only_mdd=mp["max_drawdown"],
                      dip_cagr=md["cagr"], dip_mdd=md["max_drawdown"],
                      bh_cagr=mb["cagr"], bh_mdd=mb["max_drawdown"], bh_sharpe=mb["sharpe"]))
start_tbl = pd.DataFrame(srows)
start_tbl.to_csv(OUT / "start_date_robustness.csv", index=False)

# --------------------------------------------------------------------------- #
# 4) Sensitivity grids
# --------------------------------------------------------------------------- #
grid = []
for drop in [0.03, 0.05, 0.07, 0.10]:
    for pdlt in [0.16, 0.25, 0.35, 0.50]:
        c = replace(BASE, drop_trigger=drop, put_delta=pdlt)
        r = wb.run(df, c)
        m, t = wb.metrics(r["equity"].equity), wb.trade_summary(r)
        grid.append(dict(drop_trigger=drop, put_delta=pdlt, cagr=m["cagr"], max_dd=m["max_drawdown"],
                         sharpe=m["sharpe"], puts_sold=t["puts_sold"],
                         assignment_rate=t["put_assignment_rate"]))
grid_put = pd.DataFrame(grid)
grid_put.to_csv(OUT / "sensitivity_drop_x_putdelta.csv", index=False)

cgrid = []
for cd in [None, 0.10, 0.15, 0.20, 0.25, 0.30]:
    for dte in [7, 14, 30]:
        c = replace(BASE, sell_calls=cd is not None, call_delta=cd or 0.2, call_dte=dte)
        r = wb.run(df, c)
        m, t = wb.metrics(r["equity"].equity), wb.trade_summary(r)
        cgrid.append(dict(call_delta="none" if cd is None else cd, call_dte=dte, cagr=m["cagr"],
                          max_dd=m["max_drawdown"], sharpe=m["sharpe"],
                          call_premium=t["call_premium_usd"],
                          call_itm_paid=t["call_itm_settlement_paid_usd"]))
        if cd is None:
            break
grid_call = pd.DataFrame(cgrid)
grid_call.to_csv(OUT / "sensitivity_calls.csv", index=False)

# --------------------------------------------------------------------------- #
# 5) Charts
# --------------------------------------------------------------------------- #
eq = base["equity"]
tr = base["trades"]
fig, ax = plt.subplots(3, 1, figsize=(12, 12), sharex=True,
                       gridspec_kw=dict(height_ratios=[3, 2, 1.4]))
ax[0].plot(eq.index, eq.equity, label="Wheel strangle (base)", color="#0f766e", lw=1.8)
ax[0].plot(eq.index, results["Puts only (no covered calls), hold assigned ETH"]["equity"].equity,
           label="Puts only (no calls)", color="#7c3aed", lw=1.2)
ax[0].plot(dip.index, dip, label="Buy spot on 5% dips", color="#d97706", lw=1.2)
ax[0].plot(bh.index, bh, label="Buy & hold ETH", color="#64748b", lw=1.2, alpha=.8)
ax[0].set_yscale("log"); ax[0].set_ylabel("Equity (USD, log)")
ax[0].set_title("ETH Wheel Strangle backtest, $100k start, 2020-01-01 → 2026-10-30")
ax[0].legend(loc="upper left"); ax[0].grid(alpha=.3)

ax[1].plot(df.loc[BASE.start:].index, df.loc[BASE.start:].close, color="#334155", lw=1)
sp = tr[tr.type == "sell_put"]
asg = tr[tr.type == "put_assigned"]
ax[1].scatter(sp.date, sp.strike, marker="v", s=18, color="#0f766e", label="Put strike sold", zorder=3)
ax[1].scatter(asg.date, asg.strike, marker="o", s=36, color="#dc2626", label="Put assigned (ETH bought)", zorder=4)
ax[1].plot(eq.index, eq.avg_cost, color="#dc2626", lw=1, ls="--", label="Avg cost of ETH held")
ax[1].set_yscale("log"); ax[1].set_ylabel("ETH / USD"); ax[1].legend(loc="upper left"); ax[1].grid(alpha=.3)

ax[2].stackplot(eq.index, eq.cash.clip(lower=0), eq.eth_value, labels=["Cash", "ETH value"],
                colors=["#cbd5e1", "#0f766e"], alpha=.8)
ax[2].set_ylabel("USD"); ax[2].legend(loc="upper left"); ax[2].grid(alpha=.3)
fig.tight_layout()
fig.savefig(OUT / "equity_curve.png", dpi=130)

fig, ax = plt.subplots(figsize=(12, 4))
for lbl, s, c in [("Wheel (base)", eq.equity, "#0f766e"), ("Buy the dip", dip, "#d97706"),
                  ("Buy & hold", bh, "#64748b")]:
    ax.plot(s.index, (s / s.cummax() - 1) * 100, label=lbl, color=c, lw=1.2)
ax.set_ylabel("Drawdown %"); ax.legend(); ax.grid(alpha=.3); ax.set_title("Drawdowns")
fig.tight_layout(); fig.savefig(OUT / "drawdowns.png", dpi=130)

fig, ax = plt.subplots(figsize=(12, 3.5))
ivs = df.loc[BASE.start:]
ax.plot(ivs.index, ivs.iv, color="#7c3aed", lw=1)
ax.axvspan(ivs.index[0], pd.Timestamp("2021-03-23"), color="#f59e0b", alpha=.15, label="Proxy IV (pre-DVOL)")
ax.set_ylabel("IV %"); ax.set_title("ETH implied vol used for pricing (Deribit DVOL)"); ax.legend(); ax.grid(alpha=.3)
fig.tight_layout(); fig.savefig(OUT / "implied_vol.png", dpi=130)

# --------------------------------------------------------------------------- #
# 6) JSON dump + console
# --------------------------------------------------------------------------- #
json.dump(dict(config=base["config"], proxy_coef=df.attrs["proxy_coef"],
               variants=summary.to_dict(orient="records")),
          open(OUT / "summary.json", "w"), indent=2, default=float)

pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)
cols = ["strategy", "final_equity", "cagr", "max_drawdown", "sharpe", "puts_sold", "puts_assigned",
        "put_premium_usd", "call_premium_usd", "call_itm_settlement_paid_usd", "eth_held_end"]
print(summary[cols].to_string(index=False))
print(yr.map(fmt_pct).to_string())
print(start_tbl.to_string(index=False))
print(grid_put.pivot(index="drop_trigger", columns="put_delta", values="cagr").map(fmt_pct))
print(grid_call.to_string(index=False))


# --------------------------------------------------------------------------- #
# 7) Markdown results file
# --------------------------------------------------------------------------- #
def md_table(d: pd.DataFrame, fmts: dict | None = None) -> str:
    fmts = fmts or {}
    cols = list(d.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for _, r in d.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            f = fmts.get(c)
            cells.append(f(v) if f and pd.notna(v) else ("" if pd.isna(v) else str(v)))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)

usd = lambda v: f"${v:,.0f}"
pct = lambda v: f"{v:.1%}"
num = lambda v: f"{v:,.2f}"
intf = lambda v: f"{v:,.0f}"

v = summary[["strategy", "final_equity", "cagr", "max_drawdown", "sharpe", "put_leg_pnl_usd",
             "call_leg_pnl_usd", "puts_sold", "puts_assigned", "eth_held_end", "avg_time_in_eth"]]
md = ["# Backtest results (auto-generated by `run_backtest.py`)", "",
      f"Period {BASE.start} → {df.index[-1].date()}, start capital ${BASE.initial_capital:,.0f}.", "",
      "## Variants vs benchmarks", "",
      md_table(v, dict(final_equity=usd, cagr=pct, max_drawdown=pct, sharpe=num, put_leg_pnl_usd=usd,
                       call_leg_pnl_usd=usd, puts_sold=intf, puts_assigned=intf, eth_held_end=num,
                       avg_time_in_eth=pct)), "",
      "## Calendar-year returns", "",
      md_table(yr.reset_index(names="year"), {c: pct for c in yr.columns}), "",
      "## Start-date robustness (CAGR / max drawdown)", "",
      md_table(start_tbl, {c: (num if "sharpe" in c else pct) for c in start_tbl.columns if c != "start"}), "",
      "## Sensitivity: drop trigger × put delta (CAGR)", "",
      md_table(grid_put.pivot(index="drop_trigger", columns="put_delta", values="cagr")
               .reset_index().rename(columns=lambda c: f"Δ{c}" if isinstance(c, float) else c),
               {**{f"Δ{c}": pct for c in [0.16, 0.25, 0.35, 0.5]}, "drop_trigger": pct}), "",
      "## Sensitivity: drop trigger × put delta (max drawdown)", "",
      md_table(grid_put.pivot(index="drop_trigger", columns="put_delta", values="max_dd")
               .reset_index().rename(columns=lambda c: f"Δ{c}" if isinstance(c, float) else c),
               {**{f"Δ{c}": pct for c in [0.16, 0.25, 0.35, 0.5]}, "drop_trigger": pct}), "",
      "## Sensitivity: covered-call delta × DTE", "",
      md_table(grid_call, dict(cagr=pct, max_dd=pct, sharpe=num, call_premium=usd, call_itm_paid=usd)), ""]
(OUT / "RESULTS.md").write_text("\n".join(md))
print("wrote", OUT / "RESULTS.md")
